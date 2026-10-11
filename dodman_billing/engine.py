"""
The framework-agnostic core. Nothing here imports FastAPI or Flask - it
only knows about: a PlanRegistry, a SubscriptionRepository, a
PaystackClientProtocol, an EntitlementHooks implementation, an EmailSender,
and a callable to resolve a Paystack customer's email back to your
product's own user_id. Every product wires those five things up once and
gets the same subscribe/cancel/webhook/reconciliation behavior Vett already
has, without copying the logic.
"""
from __future__ import annotations
import datetime
import hmac
import json
import logging
from typing import Awaitable, Callable

from dodman_billing.config import BillingConfig
from dodman_billing.plans import PlanRegistry
from dodman_billing.repository import SubscriptionRepository
from dodman_billing.paystack import PaystackClientProtocol
from dodman_billing.hooks import EntitlementHooks
from dodman_billing.notifications import EmailSender, NullEmailSender, resolve_period_dates, parse_dt
from dodman_billing.emails import build_invoice_html, build_invoice_pdf, build_lifecycle_email_html
from dodman_billing.errors import BillingError
from typing import Any

log = logging.getLogger(__name__)

ResolveUserByEmail = Callable[[str], Awaitable[str | None]]
AuditLogFn = Callable[..., Awaitable[None]]


async def _noop_audit_log(*args: Any, **kwargs: Any) -> None:
    return None


class BillingEngine:
    def __init__(self, *, config: BillingConfig, plans: PlanRegistry, repository: SubscriptionRepository,
                 paystack: PaystackClientProtocol, hooks: EntitlementHooks,
                 resolve_user_by_email: ResolveUserByEmail, resolve_email_by_user_id: ResolveUserByEmail,
                 email_sender: EmailSender | None = None, audit_log: AuditLogFn | None = None):
        self.config = config
        self.plans = plans
        self.repo = repository
        self.paystack = paystack
        self.hooks = hooks
        self.resolve_user_by_email = resolve_user_by_email
        self.resolve_email_by_user_id = resolve_email_by_user_id
        self.emails = email_sender or NullEmailSender()
        self.audit_log = audit_log or _noop_audit_log

    async def _log(self, event: str, success: bool, *, user_type: str = "user", target_type: str = "subscription",
                    target_id: str = "", ip_address: str = "", details: dict | None = None) -> None:
        try:
            await self.audit_log(event, success, user_type=user_type, target_type=target_type,
                                  target_id=target_id, ip_address=ip_address, details=details or {})
        except Exception:
            log.exception("Audit log call failed for event=%s (billing logic still proceeded)", event)

    # ------------------------------------------------------------------
    # Outbound notifications
    # ------------------------------------------------------------------

    async def _send_invoice_email(self, *, to: str, invoice_ref: str, plan: str, amount_minor: int,
                                    currency: str, paid_at: str, period_start: str = "", period_end: str = "") -> None:
        html = build_invoice_html(self.config.branding, invoice_ref=invoice_ref, user_email=to, plan=plan,
                                   amount_minor=amount_minor, currency=currency, paid_at=paid_at,
                                   period_start=period_start, period_end=period_end)
        amount_display = f"{currency} {amount_minor / 100:,.2f}"
        await self.emails.send(to, f"Your {self.config.branding.product_display_name} invoice - {amount_display} - {invoice_ref[:12]}", html)

    async def _send_lifecycle_email(self, *, to: str, subject: str, heading: str, body: str,
                                      cta_text: str = "", cta_url: str = "") -> None:
        html = build_lifecycle_email_html(self.config.branding, heading=heading, body=body, cta_text=cta_text, cta_url=cta_url)
        await self.emails.send(to, subject, html)

    def _dashboard_url(self) -> str:
        return f"{self.config.app_base_url}{self.config.dashboard_path}"

    # ------------------------------------------------------------------
    # Subscribe / checkout
    # ------------------------------------------------------------------

    async def subscribe(self, *, user_id: str, email: str, plan_key: str) -> dict:
        plan = self.plans.get(plan_key)
        if not plan:
            raise BillingError("VALIDATION_FAILED", f"Invalid plan '{plan_key}'.", 422)

        try:
            plan_data = await self.paystack.get_plan(plan.paystack_plan_code)
        except Exception as exc:
            log.error("Paystack plan fetch failed for %s: %s", plan.paystack_plan_code, exc)
            raise BillingError("PAYMENT_GATEWAY_ERROR", "Could not retrieve plan details from Paystack.", 502)

        amount = plan_data.get("data", {}).get("amount", 0)
        if not amount or amount <= 0:
            raise BillingError("CONFIGURATION_ERROR", f"Could not determine plan amount for '{plan_key}'.", 503)

        callback_url = f"{self.config.app_base_url}/billing/callback"
        try:
            data = await self.paystack.initialize_transaction(
                email=email, amount=amount, plan_code=plan.paystack_plan_code, callback_url=callback_url,
                metadata={"user_id": user_id, "plan": plan_key, "product": self.config.product_slug},
            )
        except Exception as exc:
            log.error("Paystack initialise failed: %s", exc)
            raise BillingError("PAYMENT_GATEWAY_ERROR", "Could not initialise payment. Please try again.", 502)

        return {
            "authorization_url": data["data"]["authorization_url"],
            "access_code": data["data"]["access_code"],
            "reference": data["data"]["reference"],
        }

    # ------------------------------------------------------------------
    # Callback (browser redirect after Paystack's hosted page)
    # ------------------------------------------------------------------

    async def handle_callback(self, reference: str | None) -> dict:
        """Returns {'outcome': 'success'|'failed'|'error', 'reason': str}.
        The framework adapter turns this into whatever redirect its product uses."""
        if not reference:
            return {"outcome": "error", "reason": "missing_ref"}

        try:
            data = await self.paystack.verify_transaction(reference)
        except Exception:
            log.exception("Paystack verify failed for ref=%s", reference)
            return {"outcome": "error", "reason": "verify_failed"}

        tx = data.get("data", {})
        if tx.get("status") != "success":
            return {"outcome": "failed", "reason": tx.get("status", "unknown")}
    
        plan_obj = tx.get("plan", {}) or {}
        plan_code = plan_obj if isinstance(plan_obj, str) else plan_obj.get("plan_code", "")
        if plan_code and self.plans.get_by_code(plan_code) is None:
            log.error("handle_callback: plan code %s is not one of product=%s's plans (ref=%s)",
                      plan_code, self.config.product_slug, reference)
            return {"outcome": "error", "reason": "unknown_plan"}

        customer = tx.get("customer", {}) or {}
        cust_email = (customer.get("email") or "").lower().strip()
        if not cust_email:
            log.error("handle_callback: no customer email in verified transaction ref=%s", reference)
            return {"outcome": "error", "reason": "no_customer_email"}

        user_id = await self.resolve_user_by_email(cust_email)
        if not user_id:
            log.error("handle_callback: no user found for email=%s ref=%s", cust_email, reference)
            return {"outcome": "error", "reason": "unknown_user"}

        await self._activate_from_transaction(tx, user_id=user_id, cust_email=cust_email)
        return {"outcome": "success", "reason": ""}

    async def _activate_from_transaction(self, tx: dict, *, user_id: str, cust_email: str,
                                           event_name: str = "billing.subscribed", ip_address: str = "") -> None:
        sub = tx.get("subscription", {}) or {}
        plan_obj = tx.get("plan", {}) or {}
        if isinstance(plan_obj, str):
            plan_obj = {"plan_code": plan_obj}
        authorization = tx.get("authorization", {}) or {}
        tx_metadata = tx.get("metadata") or {}

        plan_code = plan_obj.get("plan_code", "")
        if plan_code and self.plans.get_by_code(plan_code) is None:
            log.warning("Ignoring transaction %s: plan code %s is not one of product=%s's plans.",
                        tx.get("reference", ""), plan_code, self.config.product_slug)
            return
        plan_key = self.plans.key_from_code(plan_code)
        period_start_str, period_end_str = resolve_period_dates(tx, sub, plan_obj)

        async with self.repo.transaction() as conn:
            await self.repo.upsert(
                conn, user_id,
                customer_id=str(tx.get("customer", {}).get("id", "")),
                subscription_id=str(sub.get("subscription_code") or sub.get("id") or ""),
                plan_code=plan_code, status="active", plan=plan_key,
                period_start=parse_dt(period_start_str), period_end=parse_dt(period_end_str),
                raw_event=tx, email_token=str(sub.get("email_token") or ""),
                authorization_code=str(authorization.get("authorization_code") or ""),
                auth_reusable=bool(authorization.get("reusable", False)),
            )
            await self.repo.clear_payment_failed(conn, user_id)
            await self.hooks.on_activated(conn, user_id, plan_key)
            await self._maybe_disable_prior_subscription(tx_metadata, user_id)

        await self._send_invoice_email(
            to=cust_email, invoice_ref=tx.get("reference", ""), plan=plan_key,
            amount_minor=tx.get("amount", 0), currency=tx.get("currency", ""),
            paid_at=tx.get("paid_at") or tx.get("paidAt") or "",
            period_start=period_start_str, period_end=period_end_str,
        )
        await self._log(event_name, True, target_id=user_id, ip_address=ip_address,
                         details={"plan": plan_key, "plan_code": plan_code, "reference": tx.get("reference", "")})

    async def _maybe_disable_prior_subscription(self, tx_metadata: dict, user_id: str) -> None:
        """If this checkout was tagged as a plan change (see change_plan), disable the
        OLD subscription now - otherwise Paystack keeps renewing both, double-charging."""
        if tx_metadata.get("action") not in ("upgrade", "downgrade_immediate"):
            return
        old_code = tx_metadata.get("old_subscription_code", "")
        old_token = tx_metadata.get("old_email_token", "")
        if not (old_code and old_token):
            return
        try:
            await self.paystack.disable_subscription(code=old_code, token=old_token)
        except Exception:
            log.exception("Failed to disable prior subscription %s during plan change for user=%s", old_code, user_id)

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------

    def verify_webhook_signature(self, body: bytes, signature_header: str) -> bool:
        if not self.config.paystack_webhook_secret:
            return True  # not configured - treated as "don't verify" upstream should require this in prod
        expected = hmac.new(self.config.paystack_webhook_secret.encode(), body, "sha512").hexdigest()
        return hmac.compare_digest(expected, signature_header or "")

    @staticmethod
    def _plan_code_in(data: dict) -> str:
        """Best-effort plan code from a webhook payload. Paystack puts it at
        data.plan on charge/subscription events and at data.subscription.plan
        on some others; either may be an object or a bare code string."""
        plan = data.get("plan") or (data.get("subscription") or {}).get("plan") or {}
        if isinstance(plan, str):
            return plan
        return plan.get("plan_code", "") if isinstance(plan, dict) else ""

    async def _event_belongs_to_this_product(self, data: dict) -> bool:
        metadata = data.get("metadata") or {}
        product = metadata.get("product")
        if product:
            return product == self.config.product_slug

        # No product tag. A plan code that isn't one of OURS means the event
        # belongs to another product sharing this Paystack account.
        plan_code = self._plan_code_in(data)
        if plan_code:
            return self.plans.get_by_code(plan_code) is not None

        return True  # nothing to check - fall through to email resolution

    async def handle_webhook(self, body: bytes, signature_header: str) -> None:
        if not self.verify_webhook_signature(body, signature_header):
            log.warning("Webhook signature mismatch - ignored")
            return
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            return

        event_type = event.get("event", "")
        data = event.get("data", {})
        log.info("Paystack webhook received: event=%s product=%s", event_type, self.config.product_slug)

        if not await self._event_belongs_to_this_product(data):
            log.info("Webhook ignored - not for product=%s", self.config.product_slug)
            return

        customer = data.get("customer", {}) or {}
        cust_email = (customer.get("email") or "").lower().strip()
        if not cust_email:
            return
        user_id = await self.resolve_user_by_email(cust_email)
        if not user_id:
            return

        handler = {
            "charge.success": self._on_charge_success,
            "subscription.create": self._on_subscription_create,
            "subscription.disable": self._on_subscription_disable,
            "subscription.not_renew": self._on_subscription_not_renew,
            "invoice.payment_failed": self._on_payment_failed,
        }.get(event_type)
        if handler:
            await handler(data, user_id=user_id, cust_email=cust_email)

    async def _on_charge_success(self, data: dict, *, user_id: str, cust_email: str) -> None:
        await self._activate_from_transaction(data, user_id=user_id, cust_email=cust_email,
                                                event_name="billing.charge_success", ip_address="paystack-webhook")

    async def _on_subscription_create(self, data: dict, *, user_id: str, cust_email: str) -> None:
        plan_obj = data.get("plan", {}) or {}
        if isinstance(plan_obj, str):
            plan_obj = {"plan_code": plan_obj}
        plan_code = plan_obj.get("plan_code", "")
        plan_key = self.plans.key_from_code(plan_code)

        async with self.repo.transaction() as conn:
            await self.repo.upsert(
                conn, user_id, customer_id=str(data.get("customer", {}).get("id", "")),
                subscription_id=str(data.get("subscription_code", "")), plan_code=plan_code,
                status="active", plan=plan_key, period_start=parse_dt(data.get("createdAt")),
                period_end=parse_dt(data.get("next_payment_date")), raw_event=data,
                email_token=str(data.get("email_token", "")),
            )
            await self.repo.clear_payment_failed(conn, user_id)
            await self.hooks.on_activated(conn, user_id, plan_key)

        b = self.config.branding
        await self._send_lifecycle_email(
            to=cust_email, subject=f"Welcome to {b.product_display_name} {plan_key.title()} - subscription active",
            heading="Subscription Activated",
            body=(f"<p>Your <strong>{plan_key.title()}</strong> subscription is now active.</p>"
                  f"<p>You can manage your subscription and view invoices from your dashboard.</p>"),
            cta_text="Go to Dashboard", cta_url=self._dashboard_url(),
        )
        await self._log("billing.subscription_created", True, target_id=user_id, ip_address="paystack-webhook",
                         details={"plan": plan_key})

    async def _on_subscription_disable(self, data: dict, *, user_id: str, cust_email: str) -> None:
        async with self.repo.transaction() as conn:
            await self.repo.clear_pending_change(conn, user_id, status="cancelled")
            await self.hooks.on_cancelled(conn, user_id)
        await self._send_lifecycle_email(
            to=cust_email, subject=f"Your {self.config.branding.product_display_name} subscription has been cancelled",
            heading="Subscription Cancelled",
            body="<p>Your subscription has been cancelled. You can re-subscribe at any time.</p>",
            cta_text="Re-subscribe", cta_url=self._dashboard_url(),
        )
        await self._log("billing.subscription_cancelled", True, target_id=user_id, ip_address="paystack-webhook",
                         details={"new_status": "cancelled"})

    async def _on_subscription_not_renew(self, data: dict, *, user_id: str, cust_email: str) -> None:
        async with self.repo.transaction() as conn:
            await self.repo.clear_pending_change(conn, user_id, status="non-renewing")
        await self._send_lifecycle_email(
            to=cust_email, subject=f"Your {self.config.branding.product_display_name} subscription will not renew",
            heading="Auto-Renewal Disabled",
            body="<p>Auto-renewal has been disabled. Your access remains active until the end of your current billing period.</p>",
            cta_text="Reactivate", cta_url=self._dashboard_url(),
        )

    async def _on_payment_failed(self, data: dict, *, user_id: str, cust_email: str) -> None:
        async with self.repo.transaction() as conn:
            await self.repo.mark_payment_failed(conn, user_id, data)
        await self._send_lifecycle_email(
            to=cust_email, subject=f"Action required: {self.config.branding.product_display_name} payment failed",
            heading="Payment Failed",
            body=("<p>We were unable to charge your card for your subscription renewal.</p>"
                  "<p>Please update your payment method to avoid losing access.</p>"),
            cta_text="Update Payment Method", cta_url=self._dashboard_url(),
        )
        await self._log("billing.payment_failed", False, target_id=user_id, ip_address="paystack-webhook",
                         details={"amount": data.get("amount")})

    # ------------------------------------------------------------------
    # Manual verify (missed callback recovery)
    # ------------------------------------------------------------------

    async def verify_transaction(self, *, user_id: str, email: str, reference: str) -> dict:
        try:
            data = await self.paystack.verify_transaction(reference)
        except Exception as exc:
            raise BillingError("PAYMENT_GATEWAY_ERROR", "Paystack verification request failed.", 502)

        tx = data.get("data", {})
        tx_status = tx.get("status", "")
        customer = tx.get("customer", {}) or {}
        tx_email = (customer.get("email") or "").lower().strip()

        synced = False
        if tx_status == "success":
            if tx_email and tx_email != email.lower():
                raise BillingError("FORBIDDEN", "This transaction does not belong to your account.", 403)
            await self._activate_from_transaction(tx, user_id=user_id, cust_email=email)
            synced = True

        plan_obj = tx.get("plan", {}) or {}
        if isinstance(plan_obj, str):
            plan_obj = {"plan_code": plan_obj}

        return {
            "reference": reference, "transaction_status": tx_status, "amount": tx.get("amount", 0),
            "currency": tx.get("currency", ""), "paid_at": tx.get("paid_at") or tx.get("paidAt"),
            "plan": self.plans.key_from_code(plan_obj.get("plan_code", "")),
            "synced_to_account": synced,
            "message": "Subscription activated and account updated." if synced else f"Transaction status is '{tx_status}' - no changes made.",
        }

    # ------------------------------------------------------------------
    # Cancel / reactivate
    # ------------------------------------------------------------------

    async def _ensure_credentials(self, conn, user_id: str, local) -> tuple[str, str]:
        sub_id = (local["paystack_subscription_id"] or "").strip()
        token = (local["paystack_email_token"] or "").strip()
        if sub_id and token:
            return sub_id, token

        cust_id = (local["paystack_customer_id"] or "").strip()
        if not cust_id:
            raise BillingError("SUBSCRIPTION_NOT_LINKED",
                                "Your subscription record is missing Paystack identifiers. Contact support.", 409)

        try:
            subs = await self.paystack.list_subscriptions_for_customer(cust_id)
        except Exception:
            raise BillingError("PAYMENT_GATEWAY_ERROR", "Could not look up your subscription on Paystack.", 502)

        match = next((s for s in subs if s.get("status") in ("active", "non-renewing")), subs[0] if subs else None)
        if not match:
            raise BillingError("SUBSCRIPTION_NOT_LINKED", "No matching subscription was found on Paystack.", 409)

        sub_id = str(match.get("subscription_code", "")).strip()
        token = str(match.get("email_token", "")).strip()
        if not (sub_id and token):
            raise BillingError("SUBSCRIPTION_NOT_LINKED", "Paystack did not return a complete subscription identifier.", 409)

        await conn.execute(
            "UPDATE subscriptions SET paystack_subscription_id = $1, paystack_email_token = $2, updated_at = NOW() "
            "WHERE user_id = $3 AND product = $4",
            sub_id, token, user_id, self.repo.product,
        )
        return sub_id, token

    async def cancel(self, *, user_id: str) -> dict:
        async with self.repo.transaction() as conn:
            local = await self.repo.get(conn, user_id)
            if not local:
                raise BillingError("NOT_FOUND", "No active subscription found for this account.", 404)
            if local["status"] in ("cancelled", "non-renewing"):
                return {"status": local["status"], "message": "Subscription is already cancelled or set to not renew.",
                        "access_until": local["current_period_end"].isoformat() if local["current_period_end"] else None}

            sub_id, email_token = await self._ensure_credentials(conn, user_id, local)
            try:
                await self.paystack.disable_subscription(code=sub_id, token=email_token)
            except Exception as exc:
                raise BillingError("PAYMENT_GATEWAY_ERROR", "Paystack could not process the cancellation.", 502)

            await self.repo.set_status(conn, user_id, "non-renewing")

        return {
            "status": "non-renewing",
            "access_until": local["current_period_end"].isoformat() if local["current_period_end"] else None,
            "message": "Auto-renewal disabled. Your subscription remains active until the end of the current billing period.",
        }

    async def reactivate(self, *, user_id: str) -> dict:
        async with self.repo.transaction() as conn:
            local = await self.repo.get(conn, user_id)
            if not local:
                raise BillingError("NOT_FOUND", "No subscription found for this user.", 404)
            sub_id, email_token = await self._ensure_credentials(conn, user_id, local)
            try:
                await self.paystack.enable_subscription(code=sub_id, token=email_token)
            except Exception:
                raise BillingError("PAYMENT_GATEWAY_ERROR", "Paystack could not re-enable the subscription.", 502)
            await self.repo.set_status(conn, user_id, "active")
        return {"user_id": user_id, "status": "active", "message": "Subscription re-enabled. Auto-renewal is active."}

    # ------------------------------------------------------------------
    # Change plan (upgrade / downgrade)
    # ------------------------------------------------------------------

    async def change_plan(self, *, user_id: str, email: str, target_plan_key: str, effective: str = "") -> dict:
        if target_plan_key not in self.plans:
            raise BillingError("VALIDATION_FAILED", f"Invalid plan '{target_plan_key}'.", 422)

        async with self.repo.transaction() as conn:
            local = await self.repo.get(conn, user_id)
            if not local or local["status"] not in ("active", "non-renewing"):
                raise BillingError("NO_ACTIVE_SUBSCRIPTION", "You don't have an active subscription to change.", 409)

            current_plan = local["plan"] or ""

            if target_plan_key == current_plan:
                if local["pending_plan"]:
                    sub_id, email_token = await self._ensure_credentials(conn, user_id, local)
                    try:
                        await self.paystack.enable_subscription(code=sub_id, token=email_token)
                    except Exception:
                        log.exception("Failed to re-enable subscription while cancelling scheduled downgrade.")
                    await self.repo.clear_pending_change(conn, user_id, status="active")
                    return {"status": "unchanged", "plan": current_plan,
                            "message": "Scheduled plan change cancelled - you'll stay on your current plan."}
                return {"status": "unchanged", "plan": current_plan, "message": f"You're already on the {target_plan_key.title()} plan."}

            is_upgrade = self.plans.is_upgrade(current_plan, target_plan_key)
            target_plan = self.plans.get(target_plan_key)
            sub_id, email_token = await self._ensure_credentials(conn, user_id, local)

            # Scheduled downgrade (default) - no charge now, switch at period end.
            if not is_upgrade and effective != "immediate":
                try:
                    await self.paystack.disable_subscription(code=sub_id, token=email_token)
                except Exception:
                    raise BillingError("PAYMENT_GATEWAY_ERROR", "Could not schedule the plan change.", 502)

                change_at = local["current_period_end"] or (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
                await self.repo.schedule_pending_change(conn, user_id, pending_plan=target_plan_key,
                                                          pending_plan_code=target_plan.paystack_plan_code, pending_change_at=change_at)
                return {
                    "status": "scheduled", "current_plan": current_plan, "target_plan": target_plan_key,
                    "effective_at": change_at.isoformat(),
                    "message": f"You'll keep your {current_plan.title()} access until {change_at.date().isoformat()}, then switch to {target_plan_key.title()}.",
                }

            # Immediate change (upgrade, or downgrade with effective='immediate')
            return await self._change_plan_immediate(conn, user_id=user_id, email=email, local=local,
                                                       current_plan=current_plan, target_plan_key=target_plan_key,
                                                       target_plan=target_plan, sub_id=sub_id, email_token=email_token, is_upgrade=is_upgrade)

    async def _change_plan_immediate(self, conn, *, user_id, email, local, current_plan, target_plan_key,
                                       target_plan, sub_id, email_token, is_upgrade) -> dict:
        try:
            old_plan_data = await self.paystack.get_plan(local["paystack_plan_code"]) if local["paystack_plan_code"] else {}
            new_plan_data = await self.paystack.get_plan(target_plan.paystack_plan_code)
            old_amount = int(old_plan_data.get("data", {}).get("amount", 0) or 0)
            new_amount = int(new_plan_data.get("data", {}).get("amount", 0) or 0)
        except Exception:
            log.exception("Could not fetch plan amounts for proration.")
            old_amount, new_amount = 0, 0

        now = datetime.datetime.now(datetime.timezone.utc)
        period_start, period_end = local["current_period_start"], local["current_period_end"]
        if period_start and period_end and period_end > period_start:
            total = (period_end - period_start).total_seconds()
            remaining = max((period_end - now).total_seconds(), 0)
            remaining_fraction = remaining / total if total > 0 else 1.0
        else:
            remaining_fraction = 1.0

        charge_amount = max(0, round((new_amount - old_amount) * remaining_fraction)) if (old_amount and new_amount) else new_amount

        action_tag = "upgrade" if is_upgrade else "downgrade_immediate"
        metadata = {"user_id": user_id, "plan": target_plan_key, "product": self.config.product_slug,
                    "action": action_tag, "old_subscription_code": sub_id, "old_email_token": email_token}

        if local["paystack_authorization_code"] and local["paystack_auth_reusable"]:
            try:
                charge_resp = await self.paystack.charge_authorization(
                    authorization_code=local["paystack_authorization_code"], email=email,
                    amount=max(charge_amount, 100), plan_code=target_plan.paystack_plan_code, metadata=metadata,
                )
                if (charge_resp.get("data", {}) or {}).get("status", "") != "success":
                    raise RuntimeError("charge did not succeed")
            except Exception:
                raise BillingError("PAYMENT_GATEWAY_ERROR", "Could not process the plan-change charge.", 502)

            new_sub = charge_resp.get("data", {}).get("subscription") or {}
            await self.repo.apply_plan_change(conn, user_id, plan=target_plan_key,
                                                plan_code=target_plan.paystack_plan_code,
                                                subscription_id=str(new_sub.get("subscription_code", "")))
            await self.hooks.on_activated(conn, user_id, target_plan_key)
            await self._maybe_disable_prior_subscription(metadata, user_id)

            await self._send_lifecycle_email(
                to=email, subject=f"Your {self.config.branding.product_display_name} plan has changed",
                heading="Plan Changed", body=f"<p>You're now on the {target_plan_key.title()} plan, effective immediately.</p>",
                cta_text="View Dashboard", cta_url=self._dashboard_url(),
            )
            return {"status": "changed", "plan": target_plan_key, "charged_amount": charge_amount,
                    "message": f"You're now on the {target_plan_key.title()} plan."}

        # No reusable card - fall back to checkout, same as a fresh subscribe.
        try:
            data = await self.paystack.initialize_transaction(
                email=email, amount=new_amount or 100, plan_code=target_plan.paystack_plan_code,
                callback_url=f"{self.config.app_base_url}/billing/callback", metadata=metadata,
            )
        except Exception:
            raise BillingError("PAYMENT_GATEWAY_ERROR", "Could not initialise the plan-change checkout.", 502)

        return {"status": "checkout_required", "plan": target_plan_key,
                "authorization_url": data["data"]["authorization_url"],
                "access_code": data["data"]["access_code"], "reference": data["data"]["reference"]}

    # ------------------------------------------------------------------
    # Status / portal / invoices
    # ------------------------------------------------------------------

    async def get_portal_status(self, *, user_id: str) -> dict:
        async with self.repo.transaction() as conn:
            row = await self.repo.get(conn, user_id)
        if not row:
            return {"sub_status": "none", "sub_plan": "free", "sub_period_end": None,
                    "pending_plan": "", "pending_change_at": None}
        return {
            "sub_status": row["status"], "sub_plan": row["plan"] or "free",
            "sub_period_end": row["current_period_end"].isoformat() if row["current_period_end"] else None,
            "pending_plan": row["pending_plan"] or "",
            "pending_change_at": row["pending_change_at"].isoformat() if row["pending_change_at"] else None,
        }

    async def list_invoices(self, *, email: str, page: int = 1, per_page: int = 20) -> dict:
        try:
            data = await self.paystack.list_transactions(email=email, page=page, per_page=per_page, status="success")
        except Exception:
            raise BillingError("PAYMENT_GATEWAY_ERROR", "Could not fetch invoices from Paystack.", 502)

        raw = data.get("data", []) or []
        meta = data.get("meta", {})
        invoices = []
        for tx in raw:
            tx_metadata = tx.get("metadata") or {}
            if tx_metadata.get("product") and tx_metadata.get("product") != self.config.product_slug:
                continue
            tx_customer = tx.get("customer", {}) or {}
            tx_email = (tx_customer.get("email") or "").lower().strip()
            if tx_email and tx_email != email.lower():
                continue
            plan_obj = tx.get("plan")
            plan_code = plan_obj if isinstance(plan_obj, str) else (plan_obj or {}).get("plan_code", "")
            invoices.append({
                "reference": tx.get("reference", ""), "amount_minor": tx.get("amount", 0),
                "amount_display": f"{tx.get('currency', '')} {tx.get('amount', 0) / 100:,.2f}",
                "currency": tx.get("currency", ""), "paid_at": tx.get("paid_at") or tx.get("paidAt"),
                "plan": self.plans.key_from_code(plan_code),
            })
        return {"invoices": invoices, "total": meta.get("total", len(invoices)), "page": page, "pages": meta.get("pageCount", 1)}

    async def build_invoice_files(self, *, reference: str, email: str) -> dict:
        try:
            data = await self.paystack.verify_transaction(reference)
        except Exception:
            raise BillingError("NOT_FOUND", "Invoice not found.", 404)

        tx = data.get("data", {})
        customer = tx.get("customer", {}) or {}
        tx_email = (customer.get("email") or "").lower().strip()
        if tx_email and tx_email != email.lower():
            raise BillingError("FORBIDDEN", "This invoice does not belong to your account.", 403)
        if tx.get("status") != "success":
            raise BillingError("VALIDATION_FAILED", "Invoice only available for successful transactions.", 400)

        plan_obj = tx.get("plan", {}) or {}
        sub = tx.get("subscription", {}) or {}
        if isinstance(plan_obj, str):
            plan_obj = {"plan_code": plan_obj}
        plan_key = self.plans.key_from_code(plan_obj.get("plan_code", ""))
        period_start, period_end = resolve_period_dates(tx, sub, plan_obj)

        kwargs = dict(invoice_ref=reference, user_email=email, plan=plan_key, amount_minor=tx.get("amount", 0),
                      currency=tx.get("currency", ""), paid_at=tx.get("paid_at") or tx.get("paidAt") or "",
                      period_start=period_start, period_end=period_end)
        html = build_invoice_html(self.config.branding, **kwargs)
        try:
            pdf_bytes = build_invoice_pdf(self.config.branding, **kwargs)
        except Exception:
            log.exception("PDF generation failed for reference=%s - HTML only", reference)
            pdf_bytes = None
        return {"html": html, "pdf_bytes": pdf_bytes}

    async def email_invoice(self, *, reference: str, email: str) -> None:
        files = await self.build_invoice_files(reference=reference, email=email)
        data = await self.paystack.verify_transaction(reference)
        tx = data.get("data", {})
        plan_obj = tx.get("plan", {}) or {}
        sub = tx.get("subscription", {}) or {}
        if isinstance(plan_obj, str):
            plan_obj = {"plan_code": plan_obj}
        period_start, period_end = resolve_period_dates(tx, sub, plan_obj)
        await self._send_invoice_email(
            to=email, invoice_ref=reference, plan=self.plans.key_from_code(plan_obj.get("plan_code", "")),
            amount_minor=tx.get("amount", 0), currency=tx.get("currency", ""),
            paid_at=tx.get("paid_at") or tx.get("paidAt") or "", period_start=period_start, period_end=period_end,
        )

    # ------------------------------------------------------------------
    # Reconciliation jobs - call these from your own scheduler (cron, etc)
    # ------------------------------------------------------------------

    async def process_due_plan_changes(self) -> None:
        """Applies scheduled downgrades whose effective date has arrived."""
        async with self.repo.transaction() as conn:
            rows = await self.repo.list_pending_changes_due(conn)

        for row in rows:
            user_id = row["user_id"]
            new_plan_key = row["pending_plan"]
            new_code = row["pending_plan_code"]
            auth_code = (row["paystack_authorization_code"] or "").strip()
            reusable = bool(row["paystack_auth_reusable"])
            email = await self.resolve_email_by_user_id(str(user_id))

            charged_ok = False
            if auth_code and reusable:
                try:
                    plan_info = await self.paystack.get_plan(new_code)
                    new_amount = int(plan_info.get("data", {}).get("amount", 0))
                    charge_resp = await self.paystack.charge_authorization(
                        authorization_code=auth_code, email=email or "", amount=max(new_amount, 100),
                        plan_code=new_code, metadata={"action": "scheduled_downgrade", "user_id": str(user_id), "product": self.config.product_slug},
                    )
                    if (charge_resp.get("data", {}) or {}).get("status", "") == "success":
                        new_sub = charge_resp.get("data", {}).get("subscription") or {}
                        async with self.repo.transaction() as conn:
                            await self.repo.apply_plan_change(conn, user_id, plan=new_plan_key, plan_code=new_code,
                                                                subscription_id=str(new_sub.get("subscription_code", "")))
                            await self.hooks.on_activated(conn, user_id, new_plan_key)
                        charged_ok = True
                        if email:
                            await self._send_lifecycle_email(
                                to=email, subject=f"Your {self.config.branding.product_display_name} plan has been switched",
                                heading="Plan Switched", body=f"<p>Your scheduled downgrade to the {new_plan_key.title()} plan is now active.</p>",
                                cta_text="View Dashboard", cta_url=self._dashboard_url(),
                            )
                        await self._log("billing.downgrade_applied", True, user_type="system", target_id=str(user_id),
                                         ip_address="reconciliation-job", details={"new_plan": new_plan_key})
                except Exception:
                    log.exception("Scheduled downgrade charge failed for user=%s", user_id)

            if not charged_ok:
                async with self.repo.transaction() as conn:
                    await self.repo.clear_pending_change(conn, user_id, status="cancelled")
                    await self.hooks.on_cancelled(conn, user_id)
                if email:
                    await self._send_lifecycle_email(
                        to=email, subject=f"We couldn't switch your {self.config.branding.product_display_name} plan automatically",
                        heading="Action Needed",
                        body=("<p>We tried to switch your subscription to the new plan you scheduled, but couldn't "
                              "complete the charge automatically. Your access has been paused.</p>"),
                        cta_text="Re-subscribe", cta_url=self._dashboard_url(),
                    )
                await self._log("billing.downgrade_failed", False, user_type="system", target_id=str(user_id),
                                 ip_address="reconciliation-job", details={"attempted_plan": new_plan_key})

    async def enforce_payment_grace_periods(self) -> None:
        """Revokes access for subscriptions whose grace period has expired,
        after re-verifying live with Paystack in case a webhook just arrived late."""
        async with self.repo.transaction() as conn:
            rows = await self.repo.list_grace_candidates(conn, self.config.payment_failure_grace_hours)

        for row in rows:
            user_id = row["user_id"]
            sub_id = (row["paystack_subscription_id"] or "").strip()

            if sub_id:
                try:
                    live = await self.paystack.get_subscription(sub_id)
                    if (live.get("data", {}) or {}).get("status", "") == "active":
                        continue  # still genuinely active - a webhook was just late
                except Exception:
                    log.warning("Could not re-verify subscription=%s for user=%s before grace revocation.", sub_id, user_id)

            async with self.repo.transaction() as conn:
                await self.repo.set_status(conn, user_id, "cancelled")
                await self.hooks.on_cancelled(conn, user_id)

            if sub_id:
                try:
                    await self.paystack.disable_subscription(code=sub_id, token="")
                except Exception:
                    pass  # best-effort - local revocation already happened, which is what matters

            email = await self.resolve_email_by_user_id(str(user_id))
            if email:
                await self._send_lifecycle_email(
                    to=email, subject=f"Your {self.config.branding.product_display_name} subscription has been suspended",
                    heading="Subscription Suspended",
                    body=("<p>We were unable to confirm an active subscription or successful payment for your "
                          "account, so access has been suspended.</p><p>Re-subscribe at any time to restore access.</p>"),
                    cta_text="Re-subscribe", cta_url=self._dashboard_url(),
                )
            await self._log("billing.access_revoked_grace_expired", True, user_type="system", target_id=str(user_id),
                             ip_address="reconciliation-job", details={"prior_status": row["status"]})
