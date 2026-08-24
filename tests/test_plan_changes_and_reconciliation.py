import asyncio
import sys
import datetime
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

    plans = PlanRegistry([Plan("starter", "PLN_starter", 1), Plan("pro", "PLN_pro", 2)])
    resolver = SharedUsersResolver(pool)
    paystack = FakePaystackClient()
    paystack.seed_plan("PLN_starter", 200000)
    paystack.seed_plan("PLN_pro", 500000)
    hooks = RecordingHooks()

    config = BillingConfig(product_slug="testproduct", paystack_secret_key="sk_fake",
                            paystack_webhook_secret="whsec_fake", app_base_url="https://example.test",
                            branding=BrandingConfig(product_display_name="Test Product"))
    repo = SubscriptionRepository(pool, product="testproduct")
    engine = BillingEngine(config=config, plans=plans, repository=repo, paystack=paystack, hooks=hooks,
                            resolve_user_by_email=resolver.user_id_by_email,
                            resolve_email_by_user_id=resolver.email_by_user_id,
                            email_sender=NullEmailSender())
    await repo.init_schema()

    async with pool.acquire() as conn:
        row = await conn.fetchrow("INSERT INTO users (email) VALUES ($1) RETURNING id", "plan-change-test@example.com")
    user_id = str(row["id"])
    email = "plan-change-test@example.com"

    # Get them onto 'starter' first
    result = await engine.subscribe(user_id=user_id, email=email, plan_key="starter")
    paystack.mark_paid(result["reference"])
    await engine.handle_callback(result["reference"])
    assert hooks.activated[-1] == (user_id, "starter")
    print("Set up: user is on 'starter'")

    # ============================================================
    # TEST: Immediate upgrade (has a reusable card on file)
    # ============================================================
    change_result = await engine.change_plan(user_id=user_id, email=email, target_plan_key="pro")
    print(f"\nUpgrade starter->pro: {change_result}")
    assert change_result["status"] == "changed"
    assert hooks.activated[-1] == (user_id, "pro")
    async with repo.transaction() as conn:
        row = await repo.get(conn, user_id)
    assert row["plan"] == "pro" and row["status"] == "active"
    print(f"Confirmed DB row: plan={row['plan']} status={row['status']}")
    print("CONFIRMED: immediate upgrade charged, activated new plan, entitlement hook fired")

    # ============================================================
    # TEST: Scheduled downgrade (pro -> starter, default behavior)
    # ============================================================
    downgrade_result = await engine.change_plan(user_id=user_id, email=email, target_plan_key="starter")
    print(f"\nScheduled downgrade pro->starter: {downgrade_result}")
    assert downgrade_result["status"] == "scheduled"
    async with repo.transaction() as conn:
        row = await repo.get(conn, user_id)
    assert row["status"] == "non-renewing" and row["pending_plan"] == "starter" and row["plan"] == "pro"
    print(f"Confirmed: still on 'pro' until period end, pending_plan='starter', status={row['status']}")
    assert hooks.activated[-1] == (user_id, "pro"), "Scheduled downgrade should NOT change entitlement yet"
    print("CONFIRMED: scheduled downgrade did not touch entitlement early - user keeps 'pro' access until period end")

    # Force the pending change to be "due now" so the reconciliation sweep picks it up
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_product', 'testproduct', true)")
            await conn.execute("UPDATE subscriptions SET pending_change_at = NOW() - INTERVAL '1 hour' WHERE user_id = $1", user_id)

    await engine.process_due_plan_changes()
    async with repo.transaction() as conn:
        row = await repo.get(conn, user_id)
    print(f"\nAfter reconciliation sweep: plan={row['plan']} status={row['status']} pending_plan={row['pending_plan']!r}")
    assert row["plan"] == "starter" and row["pending_plan"] == ""
    assert hooks.activated[-1] == (user_id, "starter")
    print("CONFIRMED: scheduled downgrade applied by reconciliation job, entitlement switched correctly")

    # ============================================================
    # TEST: Grace period enforcement (payment failure -> revoke after grace window)
    # ============================================================
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_product', 'testproduct', true)")
            await conn.execute(
                "UPDATE subscriptions SET status = 'attention', payment_failed_at = NOW() - INTERVAL '100 hours' WHERE user_id = $1",
                user_id,
            )
    # Also reflect the failure on the fake Paystack side - otherwise the
    # engine's "re-verify before revoking" safety net (correctly) assumes a
    # webhook just arrived late and declines to revoke, which is exactly
    # the behavior test 4 (below) checks for. This test wants a genuine
    # expiry: Paystack no longer considers the subscription active either.
    async with repo.transaction() as conn:
        current = await repo.get(conn, user_id)
    live_sub_id = current["paystack_subscription_id"]
    if live_sub_id in paystack.subscriptions:
        paystack.subscriptions[live_sub_id]["status"] = "non-renewing"
    hooks_cancelled_before = list(hooks.cancelled)
    await engine.enforce_payment_grace_periods()
    print(f"\nAfter grace-period sweep: cancelled={hooks.cancelled}")
    assert hooks.cancelled == hooks_cancelled_before + [user_id]
    async with repo.transaction() as conn:
        row = await repo.get(conn, user_id)
    assert row["status"] == "cancelled"
    print("CONFIRMED: expired grace period correctly revoked access")

    # ============================================================
    # TEST: Grace period does NOT revoke if Paystack says still active
    # (the "webhook just arrived late" safety net)
    # ============================================================
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_product', 'testproduct', true)")
            # re-activate them with a real subscription_id Paystack will report as active
            await conn.execute(
                "UPDATE subscriptions SET status = 'attention', payment_failed_at = NOW() - INTERVAL '100 hours', "
                "paystack_subscription_id = 'SUB_still_active' WHERE user_id = $1", user_id,
            )
    paystack.subscriptions["SUB_still_active"] = {"subscription_code": "SUB_still_active", "status": "active"}
    hooks_cancelled_before = list(hooks.cancelled)
    await engine.enforce_payment_grace_periods()
    assert hooks.cancelled == hooks_cancelled_before, "Should NOT have revoked - Paystack said still active!"
    print("\nCONFIRMED: grace sweep correctly skipped revocation when Paystack still reports the subscription active")

    await pool.close()
    print("\nALL PLAN-CHANGE AND RECONCILIATION TESTS PASSED")


asyncio.run(main())
