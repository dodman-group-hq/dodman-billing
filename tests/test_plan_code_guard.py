import asyncio
import json
import hmac
import sys
sys.path.insert(0, '.')

import asyncpg

from dodman_billing.config import BillingConfig, BrandingConfig
from dodman_billing.plans import Plan, PlanRegistry
from dodman_billing.repository import SubscriptionRepository
from dodman_billing.engine import BillingEngine
from dodman_billing.identity import SharedUsersResolver
from dodman_billing.notifications import NullEmailSender

from tests.fakes import FakePaystackClient, RecordingHooks

DSN = "postgresql://sgapp:testpass@localhost:5432/billing_test"


async def main():
    pool = await asyncpg.create_pool(DSN)
    resolver = SharedUsersResolver(pool)

    sg_plans = PlanRegistry([Plan("paid", "PLN_sg_paid", 1)])
    sg_hooks = RecordingHooks()
    config = BillingConfig(product_slug="statement-generator", paystack_secret_key="sk_fake",
                           paystack_webhook_secret="whsec_fake", app_base_url="https://example.test",
                           branding=BrandingConfig(product_display_name="Statement Generator"))
    repo = SubscriptionRepository(pool, product="statement-generator")
    await repo.init_schema()
    sg_engine = BillingEngine(config=config, plans=sg_plans, repository=repo, paystack=FakePaystackClient(),
                              hooks=sg_hooks, resolve_user_by_email=resolver.user_id_by_email,
                              resolve_email_by_user_id=resolver.email_by_user_id, email_sender=NullEmailSender())

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE email = $1", "guard-test@example.com")
        await conn.fetchrow("INSERT INTO users (email) VALUES ($1) RETURNING id", "guard-test@example.com")

    # A Vett renewal: NO metadata, but a plan code that is not SG's.
    vett_renewal = {
        "event": "charge.success",
        "data": {"status": "success", "reference": "vett_renewal_1", "amount": 200000, "currency": "KES",
                 "customer": {"id": 1, "email": "guard-test@example.com"},
                 "plan": {"plan_code": "PLN_vett_starter"}},
    }
    body = json.dumps(vett_renewal).encode()
    sig = hmac.new(b"whsec_fake", body, "sha512").hexdigest()
    await sg_engine.handle_webhook(body, sig)
    assert sg_hooks.activated == [], "SG activated a plan from a Vett renewal!"
    print("CONFIRMED: untagged event for another product's plan code was ignored")

    # Same event but with SG's own plan code must still be processed.
    own = json.loads(body)
    own["data"]["plan"]["plan_code"] = "PLN_sg_paid"
    own["data"]["reference"] = "sg_renewal_1"
    body2 = json.dumps(own).encode()
    sig2 = hmac.new(b"whsec_fake", body2, "sha512").hexdigest()
    await sg_engine.handle_webhook(body2, sig2)
    assert len(sg_hooks.activated) == 1 and sg_hooks.activated[0][1] == "paid"
    print("CONFIRMED: an untagged event for this product's own plan is still processed")

    await pool.close()
    print("\nPLAN CODE GUARD TESTS PASSED")


asyncio.run(main())