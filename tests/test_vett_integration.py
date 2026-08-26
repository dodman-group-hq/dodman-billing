"""
This is the closest I can get to "run it against Vett" without your actual
app.py - a FastAPI app built the same way yours almost certainly is
(lifespan-created pool, session-based get_current_user, header-based
verify_internal_secret, a _write_audit_log(pool, event, success, **kw)
helper, an _error_envelope(code, message, request_id) helper), wired to
REAL api_keys and audit_log tables with your actual column shapes, using
your REAL plan structure via examples/vett_wiring.py's VettEntitlementHooks.

Everything below the "=== Stand-ins for your app's real dependencies ==="
marker is inferred from how your original billing_router.py called these
functions - if any of these don't match your actual implementations, the
INTEGRATION SHAPE this proves (how billing wires into a FastAPI app with
your dependency patterns) still holds; only the specific stand-in would
need correcting.
"""
import asyncio
import sys
import json
sys.path.insert(0, '.')
sys.path.insert(0, 'examples')

import asyncpg
import httpx
from fastapi import FastAPI, Request, Depends, HTTPException, Header

from dodman_billing import (
    BillingConfig, BrandingConfig, Plan, PlanRegistry,
    SubscriptionRepository, SharedUsersResolver, BillingEngine, build_billing_router,
)
from dodman_billing.notifications import NullEmailSender

from vett_wiring import VettEntitlementHooks
from tests.fakes import FakePaystackClient

DSN = "postgresql://sgapp:testpass@localhost:5432/billing_test"
INTERNAL_SECRET = "test-internal-secret"


# === Stand-ins for your app's real dependencies ============================

def make_get_current_user(pool: asyncpg.Pool):
    """Stand-in for session-cookie auth. Real one presumably reads a session
    cookie/JWT and looks up the user - this test drives it with a header
    instead, since there's no real login flow to exercise here."""
    async def get_current_user(x_test_user_email: str = Header(...)) -> dict:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, email FROM users WHERE email = $1", x_test_user_email)
        if not row:
            raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "No such user."})
        return {"id": str(row["id"]), "email": row["email"]}
    return get_current_user


async def verify_internal_secret(x_internal_secret: str = Header(default="")) -> bool:
    if x_internal_secret != INTERNAL_SECRET:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "Invalid internal secret."})
    return True


def make_write_audit_log(pool: asyncpg.Pool):
    """Matches your original _write_audit_log(pool, event, success, user_type=,
    target_type=, target_id=, ip_address=, details=) signature exactly -
    engine.py and router.py both call audit_log with this same shape now, so
    your real function can be passed in with zero wrapping needed."""
    async def write_audit_log(event: str, success: bool, *, user_type: str = "", target_type: str = "",
                               target_id: str = "", ip_address: str = "", details: dict | None = None) -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO audit_log (event, success, user_type, target_type, target_id, ip_address, details) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)",
                event, success, user_type, target_type, str(target_id), ip_address, json.dumps(details or {}),
            )
    return write_audit_log


def error_envelope(code: str, message: str, request_id: str | None) -> dict:
    """Stand-in for your real _error_envelope - shape guessed as plausible,
    but the point is: router.py now calls whatever function you give it with
    exactly (code, message, request_id), so your real one slots in as-is."""
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def get_request_id(request: Request) -> str | None:
    return request.headers.get("x-request-id", "test-request-id")


# === Test =====================================================================

async def main():
    pool = await asyncpg.create_pool(DSN)

    paystack = FakePaystackClient()
    paystack.seed_plan("PLN_vett_starter_TEST", 200000)
    paystack.seed_plan("PLN_vett_pro_TEST", 500000)

    plans = PlanRegistry([
        Plan(key="starter", paystack_plan_code="PLN_vett_starter_TEST", rank=1),
        Plan(key="pro", paystack_plan_code="PLN_vett_pro_TEST", rank=2),
    ])
    config = BillingConfig(
        product_slug="vett", paystack_secret_key="sk_fake", paystack_webhook_secret="whsec_fake",
        app_base_url="https://vett.test", branding=BrandingConfig(product_display_name="Vett"),
    )
    repository = SubscriptionRepository(pool, product="vett")
    await repository.init_schema()
    resolver = SharedUsersResolver(pool)
    write_audit_log = make_write_audit_log(pool)

    engine = BillingEngine(
        config=config, plans=plans, repository=repository, paystack=paystack,
        hooks=VettEntitlementHooks(), resolve_user_by_email=resolver.user_id_by_email,
        resolve_email_by_user_id=resolver.email_by_user_id, email_sender=NullEmailSender(),
        audit_log=write_audit_log,   # <- internal webhook/reconciliation events logged here
    )

    app = FastAPI()
    router = build_billing_router(
        engine=engine, get_current_user=make_get_current_user(pool), verify_internal_secret=verify_internal_secret,
        audit_log=write_audit_log,   # <- same function, request-driven events logged here
        error_envelope=error_envelope, get_request_id=get_request_id,
    )
    app.include_router(router, prefix="/billing")

    # --- Set up a real user + a real Vett-shaped api_keys row -----------------
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM api_keys WHERE user_id IN (SELECT id FROM users WHERE email = $1)", "vett-user@example.com")
        await conn.execute("DELETE FROM users WHERE email = $1", "vett-user@example.com")
        row = await conn.fetchrow("INSERT INTO users (email) VALUES ($1) RETURNING id", "vett-user@example.com")
        user_id = str(row["id"])
        await conn.execute(
            "INSERT INTO api_keys (user_id, key_hash, environment, tier, is_active) VALUES ($1,$2,'live','free',TRUE)",
            row["id"], "fake_hash",
        )

    print(f"Set up Vett-shaped user + api_keys row: user_id={user_id}, initial tier=free")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Test-User-Email": "vett-user@example.com"}

        # 1. Subscribe
        r = await client.post("/billing/subscribe", json={"plan": "starter"}, headers=headers)
        print(f"\nPOST /billing/subscribe -> {r.status_code} {r.json()}")
        assert r.status_code == 200
        reference = r.json()["reference"]

        # 2. Complete "payment", hit callback
        paystack.mark_paid(reference)
        r = await client.get(f"/billing/callback?reference={reference}", headers=headers, follow_redirects=False)
        print(f"GET /billing/callback -> {r.status_code} {r.headers.get('location')}")
        assert r.status_code == 303

        # 3. THE REAL TEST: did api_keys.tier actually get updated to 'starter'?
        async with pool.acquire() as conn:
            key_row = await conn.fetchrow("SELECT tier, is_active FROM api_keys WHERE user_id = $1", user_id)
        print(f"api_keys row after activation: tier={key_row['tier']} is_active={key_row['is_active']}")
        assert key_row["tier"] == "starter"
        print("CONFIRMED: Vett's real api_keys.tier column was updated by the entitlement hook")

        # 4. Confirm audit log entries landed with the right shape
        async with pool.acquire() as conn:
            audit_rows = await conn.fetch("SELECT event, success, target_id, ip_address FROM audit_log WHERE target_id = $1 ORDER BY id", user_id)
        print(f"\nAudit log entries for this user: {[dict(r) for r in audit_rows]}")
        events = [r["event"] for r in audit_rows]
        assert "billing.subscribe_initiated" in events
        assert "billing.subscribed" in events
        print("CONFIRMED: audit log entries written via the SAME write_audit_log function for both router and engine events")

        # 5. Error envelope shape - confirm YOUR shape (not the package's default) came through
        r = await client.post("/billing/subscribe", json={"plan": "nonexistent"}, headers=headers)
        print(f"\nBad plan -> {r.status_code} {r.json()}")
        assert r.status_code == 422
        assert "error" in r.json()["detail"], "Custom error_envelope shape was not used!"
        assert r.json()["detail"]["error"]["request_id"] == "test-request-id"
        print("CONFIRMED: custom error_envelope shape used, request_id threaded through correctly")

        # 6. Cancel -> webhook -> confirm api_keys.is_active flips to FALSE
        r = await client.post("/billing/cancel", headers=headers)
        print(f"\nPOST /billing/cancel -> {r.status_code} {r.json()}")
        assert r.status_code == 200

        import hmac
        disable_event = {"event": "subscription.disable", "data": {"customer": {"email": "vett-user@example.com"}, "metadata": {"product": "vett"}}}
        body = json.dumps(disable_event).encode()
        sig = hmac.new(config.paystack_webhook_secret.encode(), body, "sha512").hexdigest()
        r = await client.post("/billing/webhook", content=body, headers={"x-paystack-signature": sig})
        print(f"POST /billing/webhook (subscription.disable) -> {r.status_code}")
        assert r.status_code == 200

        async with pool.acquire() as conn:
            key_row = await conn.fetchrow("SELECT tier, is_active FROM api_keys WHERE user_id = $1", user_id)
        print(f"api_keys row after cancellation webhook: tier={key_row['tier']} is_active={key_row['is_active']}")
        assert key_row["is_active"] is False
        print("CONFIRMED: Vett's live API key was correctly deactivated on cancellation")

        async with pool.acquire() as conn:
            audit_rows = await conn.fetch("SELECT event FROM audit_log WHERE target_id = $1 ORDER BY id", user_id)
        assert "billing.subscription_cancelled" in [r["event"] for r in audit_rows]
        print("CONFIRMED: webhook-triggered cancellation was also audit-logged")

    await pool.close()
    print("\n" + "=" * 70)
    print("ALL VETT INTEGRATION TESTS PASSED")
    print("=" * 70)


asyncio.run(main())
