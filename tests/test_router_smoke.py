import asyncio
import sys
sys.path.insert(0, '.')

import asyncpg
import httpx
from fastapi import FastAPI, Depends, HTTPException

from dodman_billing.config import BillingConfig, BrandingConfig
from dodman_billing.plans import Plan, PlanRegistry
from dodman_billing.repository import SubscriptionRepository
from dodman_billing.engine import BillingEngine
from dodman_billing.identity import SharedUsersResolver
from dodman_billing.notifications import NullEmailSender
from dodman_billing.router import build_billing_router

from tests.fakes import FakePaystackClient, RecordingHooks

DSN = "postgresql://sgapp:testpass@localhost:5432/billing_test"


async def setup():
    pool = await asyncpg.create_pool(DSN)
    plans = PlanRegistry([Plan("starter", "PLN_starter", 1), Plan("pro", "PLN_pro", 2)])
    resolver = SharedUsersResolver(pool)
    paystack = FakePaystackClient()
    paystack.seed_plan("PLN_starter", 200000)
    hooks = RecordingHooks()

    config = BillingConfig(product_slug="routertest", paystack_secret_key="sk_fake",
                            paystack_webhook_secret="whsec_fake", app_base_url="https://example.test",
                            branding=BrandingConfig(product_display_name="Router Test"))
    repo = SubscriptionRepository(pool, product="routertest")
    engine = BillingEngine(config=config, plans=plans, repository=repo, paystack=paystack, hooks=hooks,
                            resolve_user_by_email=resolver.user_id_by_email,
                            resolve_email_by_user_id=resolver.email_by_user_id,
                            email_sender=NullEmailSender())
    await repo.init_schema()

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE email = $1", "router-test@example.com")
        row = await conn.fetchrow("INSERT INTO users (email) VALUES ($1) RETURNING id", "router-test@example.com")
    user_id = str(row["id"])

    return pool, engine, user_id, paystack


async def main():
    pool, engine, user_id, paystack = await setup()

    # Fake auth dependencies, standing in for a product's real session/admin auth
    async def get_current_user():
        return {"id": user_id, "email": "router-test@example.com"}

    async def verify_internal_secret():
        return True

    app = FastAPI()
    router = build_billing_router(engine=engine, get_current_user=get_current_user, verify_internal_secret=verify_internal_secret)
    app.include_router(router, prefix="/billing")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # POST /billing/subscribe
        r = await client.post("/billing/subscribe", json={"plan": "starter"})
        print("POST /billing/subscribe ->", r.status_code, r.json())
        assert r.status_code == 200
        reference = r.json()["reference"]

        # Simulate payment completion, then hit the callback
        paystack.mark_paid(reference)
        r = await client.get(f"/billing/callback?reference={reference}", follow_redirects=False)
        print("GET /billing/callback ->", r.status_code, r.headers.get("location"))
        assert r.status_code in (303, 307)
        assert "billing=success" in r.headers.get("location", "")

        # GET /billing/portal
        r = await client.get("/billing/portal")
        print("GET /billing/portal ->", r.status_code, r.json())
        assert r.status_code == 200
        assert r.json()["sub_status"] == "active"
        assert r.json()["sub_plan"] == "starter"

        # Invalid plan -> should come back as a clean 422, not a 500
        r = await client.post("/billing/subscribe", json={"plan": "does-not-exist"})
        print("POST /billing/subscribe (bad plan) ->", r.status_code, r.json())
        assert r.status_code == 422

        # POST /billing/cancel
        r = await client.post("/billing/cancel")
        print("POST /billing/cancel ->", r.status_code, r.json())
        assert r.status_code == 200
        assert r.json()["status"] == "non-renewing"

        # Admin-only route without the dependency satisfied would 401/403 in a real
        # app; here it's wired to always pass, so just confirm it's reachable and
        # rejects a request with no user_id in the body.
        r = await client.post("/billing/reactivate", json={})
        print("POST /billing/reactivate (no user_id) ->", r.status_code, r.json())
        assert r.status_code == 422

    await pool.close()
    print("\nALL ROUTER SMOKE TESTS PASSED")


asyncio.run(main())
