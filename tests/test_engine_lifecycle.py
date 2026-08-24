import asyncio
import sys
import uuid
sys.path.insert(0, '.')

import asyncpg

from dodman_billing.config import BillingConfig, BrandingConfig
from dodman_billing.plans import Plan, PlanRegistry
from dodman_billing.repository import SubscriptionRepository
from dodman_billing.engine import BillingEngine
from dodman_billing.identity import SharedUsersResolver
from dodman_billing.notifications import NullEmailSender
from dodman_billing.errors import BillingError

from tests.fakes import FakePaystackClient, RecordingHooks

DSN = "postgresql://sgapp:testpass@localhost:5432/billing_test"


def make_engine(pool, product_slug, plans, hooks, paystack, resolver):
    config = BillingConfig(
        product_slug=product_slug,
        paystack_secret_key="sk_test_fake",
        paystack_webhook_secret="whsec_fake",
        app_base_url="https://example.test",
        branding=BrandingConfig(product_display_name=product_slug.title()),
    )
    repo = SubscriptionRepository(pool, product=product_slug)
    return config, repo, BillingEngine(
        config=config, plans=plans, repository=repo, paystack=paystack, hooks=hooks,
        resolve_user_by_email=resolver.user_id_by_email,
        resolve_email_by_user_id=resolver.email_by_user_id,
        email_sender=NullEmailSender(),
    )


async def main():
    pool = await asyncpg.create_pool(DSN)

    # --- Set up two products' worth of plans ---
    vett_plans = PlanRegistry([Plan("starter", "PLN_vett_starter", 1), Plan("pro", "PLN_vett_pro", 2)])
    sg_plans = PlanRegistry([Plan("starter", "PLN_sg_starter", 1), Plan("paid", "PLN_sg_paid", 2)])

    resolver = SharedUsersResolver(pool)

    paystack = FakePaystackClient()
    paystack.seed_plan("PLN_vett_starter", 200000)  # 2,000.00 KES
    paystack.seed_plan("PLN_vett_pro", 500000)
    paystack.seed_plan("PLN_sg_starter", 100000)
    paystack.seed_plan("PLN_sg_paid", 300000)

    vett_hooks = RecordingHooks()
    sg_hooks = RecordingHooks()

    vett_config, vett_repo, vett_engine = make_engine(pool, "vett", vett_plans, vett_hooks, paystack, resolver)
    sg_config, sg_repo, sg_engine = make_engine(pool, "statement-generator", sg_plans, sg_hooks, paystack, resolver)

    await vett_repo.init_schema()  # idempotent, shared table

    # --- Create a user in the shared users table ---
    async with pool.acquire() as conn:
        row = await conn.fetchrow("INSERT INTO users (email) VALUES ($1) RETURNING id", "jane@example.com")
    user_id = str(row["id"])
    print(f"Created shared user: {user_id}")

    # ============================================================
    # TEST 1: Subscribe to Vett, complete "payment", confirm activation
    # ============================================================
    result = await vett_engine.subscribe(user_id=user_id, email="jane@example.com", plan_key="starter")
    ref = result["reference"]
    print(f"\n[Vett] subscribe() -> reference={ref}")

    paystack.mark_paid(ref)  # simulate completing Paystack's hosted checkout
    cb = await vett_engine.handle_callback(ref)
    print(f"[Vett] callback outcome: {cb}")
    assert cb["outcome"] == "success"
    assert vett_hooks.activated == [(user_id, "starter")], vett_hooks.activated
    print("[Vett] entitlement hook fired correctly: activated at 'starter'")

    async with vett_repo.transaction() as conn:
        vett_row = await vett_repo.get(conn, user_id)
    assert vett_row["status"] == "active" and vett_row["plan"] == "starter"
    print(f"[Vett] subscription row: status={vett_row['status']} plan={vett_row['plan']}")

    # ============================================================
    # TEST 2: SAME user subscribes to Statement Generator too - must not
    # collide with or overwrite the Vett subscription (the composite-key fix)
    # ============================================================
    result2 = await sg_engine.subscribe(user_id=user_id, email="jane@example.com", plan_key="starter")
    ref2 = result2["reference"]
    paystack.mark_paid(ref2)
    cb2 = await sg_engine.handle_callback(ref2)
    assert cb2["outcome"] == "success"
    print(f"\n[SG] subscribed same user independently: {cb2}")

    async with vett_repo.transaction() as conn:
        vett_row_after = await vett_repo.get(conn, user_id)
    async with sg_repo.transaction() as conn:
        sg_row = await sg_repo.get(conn, user_id)

    assert vett_row_after["plan"] == "starter", "Vett's subscription was clobbered by SG's subscribe!"
    assert sg_row["plan"] == "starter"
    assert vett_row_after["paystack_subscription_id"] != sg_row["paystack_subscription_id"]
    print(f"[Vett] still: plan={vett_row_after['plan']} sub_id={vett_row_after['paystack_subscription_id']}")
    print(f"[SG]   now:   plan={sg_row['plan']} sub_id={sg_row['paystack_subscription_id']}")
    print("CONFIRMED: one user, two independent product subscriptions, no collision")

    # ============================================================
    # TEST 3: Cross-product webhook safety - a webhook tagged for Vett must
    # never touch the Statement Generator engine's data, even if somehow
    # routed to it (simulating the "one Paystack account, N products" reality)
    # ============================================================
    import json as _json, hmac as _hmac
    vett_charge_event = {
        "event": "charge.success",
        "data": {**paystack.transactions[ref], "metadata": {"product": "vett", "user_id": user_id}},
    }
    body = _json.dumps(vett_charge_event).encode()
    sig = _hmac.new(sg_config.paystack_webhook_secret.encode(), body, "sha512").hexdigest()

    sg_hooks_before = list(sg_hooks.activated)
    await sg_engine.handle_webhook(body, sig)  # delivered to the WRONG engine on purpose
    assert sg_hooks.activated == sg_hooks_before, "SG engine processed a webhook meant for Vett!"
    print("\nCONFIRMED: webhook tagged for 'vett' was correctly ignored by the 'statement-generator' engine")

    # ============================================================
    # TEST 4: Cancel -> subscription.disable webhook -> entitlement revoked
    # ============================================================
    cancel_result = await vett_engine.cancel(user_id=user_id)
    print(f"\n[Vett] cancel() -> {cancel_result}")
    assert cancel_result["status"] == "non-renewing"
    assert vett_hooks.cancelled == [], "Cancel alone shouldn't revoke access yet - only the disable webhook should"

    disable_event = {"event": "subscription.disable",
                      "data": {"customer": {"email": "jane@example.com"}, "metadata": {"product": "vett"}}}
    body2 = _json.dumps(disable_event).encode()
    sig2 = _hmac.new(vett_config.paystack_webhook_secret.encode(), body2, "sha512").hexdigest()
    await vett_engine.handle_webhook(body2, sig2)
    assert vett_hooks.cancelled == [user_id]
    print("[Vett] subscription.disable webhook correctly revoked entitlement")

    # SG's entitlement must be completely untouched by Vett's cancellation
    assert sg_hooks.cancelled == []
    print("[SG] confirmed unaffected by Vett's cancellation")

    await pool.close()
    print("\nALL ENGINE LIFECYCLE TESTS PASSED")


asyncio.run(main())
