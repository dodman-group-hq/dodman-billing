"""
FastAPI adapter. This file is the only place that knows about FastAPI -
BillingEngine itself has no framework dependency. Route paths and response
shapes deliberately match the original router closely, so swapping Vett
over to this is a mechanical change, not a rewrite of its frontend.

Usage (mirrors how the original router was mounted):

    from dodman_billing import build_billing_router

    router = build_billing_router(
        engine=my_billing_engine,
        get_current_user=get_current_user,       # your existing FastAPI dependency
        verify_internal_secret=verify_internal_secret,  # your existing admin-auth dependency
        audit_log=my_write_audit_log,             # optional: async fn(event, success, **kw)
        error_envelope=my_error_envelope,         # optional: fn(code, message, request_id) -> Any
        get_request_id=my_get_request_id,         # optional: fn(Request) -> str
    )
    app.include_router(router, prefix="/billing", tags=["billing"])

error_envelope and get_request_id exist so an existing product's error
response SHAPE doesn't silently change underneath its frontend just because
the billing logic moved into this package - if you already have
_error_envelope(code, message, request_id), pass it straight through and
every error this router raises comes back looking exactly like it did
before.
"""
from __future__ import annotations
from typing import Callable, Awaitable, Any
import asyncio

from fastapi import APIRouter, Request, Response, Depends, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from dodman_billing.engine import BillingEngine
from dodman_billing.errors import BillingError

AuditLogFn = Callable[..., Awaitable[None]]


async def _noop_audit_log(*args: Any, **kwargs: Any) -> None:
    return None


def _default_error_envelope(code: str, message: str, request_id: str | None) -> dict:
    return {"code": code, "message": message, "request_id": request_id}


def _default_get_request_id(request: Request) -> str | None:
    return request.headers.get("x-request-id")


def _default_client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def build_billing_router(*, engine: BillingEngine, get_current_user: Callable, verify_internal_secret: Callable,
                          audit_log: AuditLogFn | None = None,
                          error_envelope: Callable[[str, str, str | None], Any] | None = None,
                          get_request_id: Callable[[Request], str | None] | None = None,
                          client_ip: Callable[[Request], str] | None = None) -> APIRouter:
    router = APIRouter(tags=["billing"])
    log_event = audit_log or _noop_audit_log
    envelope = error_envelope or _default_error_envelope
    request_id_of = get_request_id or _default_get_request_id
    ip_of = client_ip or _default_client_ip

    def _error_response(exc: BillingError, request: Request | None = None) -> HTTPException:
        rid = request_id_of(request) if request is not None else None
        return HTTPException(status_code=exc.status_code, detail=envelope(exc.code, exc.message, rid))

    async def _log(request: Request, event: str, success: bool, *, user_type: str = "user",
                    target_type: str = "subscription", target_id: str = "", details: dict | None = None) -> None:
        try:
            await log_event(event, success, user_type=user_type, target_type=target_type, target_id=target_id,
                             ip_address=ip_of(request), details=details or {})
        except Exception:
            pass  # audit logging must never break the actual request

    # ------------------------------------------------------------------
    @router.post("/subscribe")
    async def subscribe(request: Request, current_user: dict = Depends(get_current_user)) -> JSONResponse:
        body = await request.json()
        plan_key = str(body.get("plan", "")).strip().lower()
        try:
            result = await engine.subscribe(user_id=current_user["id"], email=current_user["email"], plan_key=plan_key)
        except BillingError as exc:
            raise _error_response(exc, request)
        asyncio.ensure_future(_log(request, "billing.subscribe_initiated", True, target_id=current_user["id"], details={"plan": plan_key}))
        return JSONResponse(content=result)

    # ------------------------------------------------------------------
    @router.get("/callback", include_in_schema=False)
    async def callback(trxref: str | None = None, reference: str | None = None) -> Response:
        ref = trxref or reference
        result = await engine.handle_callback(ref)
        if result["outcome"] == "success":
            return RedirectResponse(url=f"{engine.config.app_base_url}/auth/login-page?billing=success", status_code=303)
        if result["outcome"] == "failed":
            return RedirectResponse(url=f"/dashboard/billing?status=failed&reason={result['reason']}")
        return RedirectResponse(url=f"/dashboard/billing?status=error&reason={result['reason']}")

    # ------------------------------------------------------------------
    @router.post("/webhook", include_in_schema=False)
    async def webhook(request: Request) -> JSONResponse:
        body = await request.body()
        signature = request.headers.get("x-paystack-signature", "")
        await engine.handle_webhook(body, signature)
        return JSONResponse(content={"status": "ok"})

    # ------------------------------------------------------------------
    @router.get("/portal")
    async def portal(current_user: dict = Depends(get_current_user)) -> JSONResponse:
        return JSONResponse(content=await engine.get_portal_status(user_id=current_user["id"]))

    # ------------------------------------------------------------------
    @router.get("/invoices")
    async def invoices(request: Request, current_user: dict = Depends(get_current_user), page: int = 1, per_page: int = 20) -> JSONResponse:
        try:
            result = await engine.list_invoices(email=current_user["email"], page=page, per_page=per_page)
        except BillingError as exc:
            raise _error_response(exc, request)
        for inv in result["invoices"]:
            inv["download_url"] = f"/billing/invoice/{inv['reference']}/download"
            inv["email_url"] = f"/billing/invoice/{inv['reference']}/email"
        return JSONResponse(content=result)

    # ------------------------------------------------------------------
    @router.get("/invoice/{reference}/download", include_in_schema=False)
    async def invoice_download(reference: str, request: Request, current_user: dict = Depends(get_current_user)) -> Response:
        try:
            files = await engine.build_invoice_files(reference=reference, email=current_user["email"])
        except BillingError as exc:
            raise _error_response(exc, request)
        asyncio.ensure_future(_log(request, "billing.invoice_downloaded", True, target_type="invoice", target_id=reference))
        if files["pdf_bytes"] is not None:
            return Response(content=files["pdf_bytes"], media_type="application/pdf",
                             headers={"Content-Disposition": f"attachment; filename=invoice-{reference[:12]}.pdf"})
        return Response(content=files["html"].encode(), media_type="text/html",
                         headers={"Content-Disposition": f"attachment; filename=invoice-{reference[:12]}.html"})

    # ------------------------------------------------------------------
    @router.post("/invoice/{reference}/email")
    async def invoice_email(reference: str, request: Request, current_user: dict = Depends(get_current_user)) -> JSONResponse:
        try:
            await engine.email_invoice(reference=reference, email=current_user["email"])
        except BillingError as exc:
            raise _error_response(exc, request)
        asyncio.ensure_future(_log(request, "billing.invoice_emailed", True, target_type="invoice", target_id=reference))
        return JSONResponse(content={"sent_to": current_user["email"], "reference": reference,
                                      "message": f"Invoice {reference[:12]}... sent to {current_user['email']}"})

    # ------------------------------------------------------------------
    @router.get("/verify/{reference}")
    async def verify(reference: str, request: Request, current_user: dict = Depends(get_current_user)) -> JSONResponse:
        try:
            result = await engine.verify_transaction(user_id=current_user["id"], email=current_user["email"], reference=reference)
        except BillingError as exc:
            raise _error_response(exc, request)
        return JSONResponse(content=result)

    # ------------------------------------------------------------------
    @router.post("/cancel")
    async def cancel(request: Request, current_user: dict = Depends(get_current_user)) -> JSONResponse:
        try:
            result = await engine.cancel(user_id=current_user["id"])
        except BillingError as exc:
            raise _error_response(exc, request)
        asyncio.ensure_future(_log(request, "billing.cancelled", True, target_id=current_user["id"]))
        return JSONResponse(content=result)

    # ------------------------------------------------------------------
    @router.post("/change-plan")
    async def change_plan(request: Request, current_user: dict = Depends(get_current_user)) -> JSONResponse:
        body = await request.json()
        target = str(body.get("plan", "")).strip().lower()
        effective = str(body.get("effective", "")).strip().lower()
        try:
            result = await engine.change_plan(user_id=current_user["id"], email=current_user["email"],
                                                target_plan_key=target, effective=effective)
        except BillingError as exc:
            raise _error_response(exc, request)
        asyncio.ensure_future(_log(request, "billing.plan_changed", True, target_id=current_user["id"], details={"target": target}))
        return JSONResponse(content=result)

    # ------------------------------------------------------------------
    @router.post("/reactivate", dependencies=[Depends(verify_internal_secret)])
    async def reactivate(request: Request) -> JSONResponse:
        body = await request.json()
        user_id = str(body.get("user_id", "")).strip()
        if not user_id:
            raise HTTPException(status_code=422, detail=envelope("VALIDATION_FAILED", "'user_id' is required.", request_id_of(request)))
        try:
            result = await engine.reactivate(user_id=user_id)
        except BillingError as exc:
            raise _error_response(exc, request)
        asyncio.ensure_future(_log(request, "billing.reactivated", True, user_type="admin", target_id=user_id))
        return JSONResponse(content=result)

    return router
