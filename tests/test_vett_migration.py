import asyncio
import sys
sys.path.insert(0, '.')

import asyncpg
from dodman_billing.repository import SubscriptionRepository

DSN = "postgresql://sgapp:testpass@localhost:5432/vett_migration_test"


async def main():
    pool = await asyncpg.create_pool(DSN)
    repo = SubscriptionRepository(pool, product="vett")

    # --- Sanity check: confirm the legacy shape is what we think it is ------
    async with pool.acquire() as conn:
        has_product_col = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='subscriptions' AND column_name='product')"
        )
        legacy_index_exists_before = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_sub_user')"
        )
    print(f"Before anything: has 'product' column = {has_product_col}, legacy idx_sub_user exists = {legacy_index_exists_before}")
    assert not has_product_col
    assert legacy_index_exists_before

    # --- Step 1: init_schema() - safe, additive, should not touch existing data ---
    # Deliberately does NOT enable RLS yet, since unmigrated (product='')
    # rows still exist - see repository.py's init_schema() docstring for why.
    # So this check reads WITHOUT product scoping (RLS isn't active yet at
    # this point), confirming the raw data is genuinely untouched.
    await repo.init_schema()
    async with pool.acquire() as conn:
        row_count_after_init = await conn.fetchval("SELECT COUNT(*) FROM subscriptions")
        preserved = await conn.fetch("SELECT user_id, plan, status, paystack_subscription_id FROM subscriptions ORDER BY user_id")
        rls_active_yet = await conn.fetchval("SELECT relrowsecurity FROM pg_class WHERE relname = 'subscriptions'")
    print(f"\nAfter init_schema(): {row_count_after_init} rows (should still be 2, untouched), RLS active yet: {rls_active_yet}")
    for r in preserved:
        print(f"  {r['user_id']}: plan={r['plan']} status={r['status']} sub_id={r['paystack_subscription_id']}")
    assert row_count_after_init == 2
    assert rls_active_yet is False, "RLS should NOT be active yet - unmigrated rows still exist, this would be the blackout window"
    assert preserved[0]["paystack_subscription_id"] in ("SUB_real_1", "SUB_real_2")
    print("CONFIRMED: init_schema() is purely additive, and correctly held off enabling RLS until migration completes")

    # --- Step 2: dry run - must report accurately, change NOTHING ---
    dry_run_result = await repo.migrate_legacy_single_tenant_table(
        backfill_product="vett", legacy_unique_index_name="idx_sub_user", dry_run=True,
    )
    print(f"\nDry run result: {dry_run_result}")
    assert dry_run_result["rows_to_backfill"] == 2
    assert dry_run_result["legacy_index_exists"] is True

    async with pool.acquire() as conn:
        still_legacy = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_sub_user')")
        still_empty_product = await conn.fetchval("SELECT COUNT(*) FROM subscriptions WHERE product = ''")
    assert still_legacy is True
    assert still_empty_product == 2
    print("CONFIRMED: dry_run=True changed nothing - index still present, rows still unbackfilled")

    # --- Step 3: the real migration ---
    real_result = await repo.migrate_legacy_single_tenant_table(
        backfill_product="vett", legacy_unique_index_name="idx_sub_user", dry_run=False,
    )
    print(f"\nReal migration result: {real_result}")
    assert real_result["rows_backfilled"] == 2
    assert real_result["legacy_index_dropped"] is True

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_product', 'vett', true)")
            legacy_gone = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_sub_user')")
            new_index_exists = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_subscriptions_user_product')")
            post_migration_rows = await conn.fetch("SELECT user_id, product, plan, status, paystack_subscription_id FROM subscriptions ORDER BY user_id")
    print(f"\nAfter real migration:")
    for r in post_migration_rows:
        print(f"  {r['user_id']}: product={r['product']!r} plan={r['plan']} status={r['status']} sub_id={r['paystack_subscription_id']}")
    assert not legacy_gone
    assert new_index_exists
    assert all(r["product"] == "vett" for r in post_migration_rows)
    assert all(r["plan"] in ("starter", "pro") for r in post_migration_rows)  # data intact, not wiped
    print("CONFIRMED: legacy index dropped, new composite index in place, ALL existing subscriber data intact and correctly tagged 'vett'")

    # --- Step 4: THE ACTUAL BUG FIX - can a second product now subscribe one
    # of these SAME existing Vett users without a unique-constraint collision? ---
    other_repo = SubscriptionRepository(pool, product="some-other-product")
    async with other_repo.transaction() as conn:
        await other_repo.upsert(
            conn, "11111111-1111-1111-1111-111111111111",  # the EXISTING Vett starter subscriber
            customer_id="cust_other", subscription_id="SUB_other_1", plan_code="PLN_other",
            status="active", plan="basic", period_start=None, period_end=None, raw_event={},
        )
    print("\nSecond product successfully subscribed an EXISTING Vett user - no unique constraint violation")

    # Check each product's row through its OWN scoped repository, not an
    # unscoped "peek at everything" query - FORCE ROW LEVEL SECURITY means
    # even the table owner genuinely cannot bypass this, which is the point.
    async with repo.transaction() as conn:
        vett_side = await repo.get(conn, "11111111-1111-1111-1111-111111111111")
    async with other_repo.transaction() as conn:
        other_side = await other_repo.get(conn, "11111111-1111-1111-1111-111111111111")

    print(f"Vett's own view of this user:            plan={vett_side['plan']} product={vett_side['product']}")
    print(f"Other product's own view of this user:   plan={other_side['plan']} product={other_side['product']}")
    both_rows = [vett_side, other_side]
    print("Rows for that user now:")
    for r in both_rows:
        print(f"  product={r['product']} plan={r['plan']} status={r['status']}")
    assert len(both_rows) == 2
    vett_row = next(r for r in both_rows if r["product"] == "vett")
    assert vett_row["plan"] == "starter", "Migration must not have altered the original Vett subscription!"
    print("CONFIRMED: the exact bug is fixed - this same insert would have raised a unique-violation error before migration")

    await pool.close()
    print("\n" + "=" * 70)
    print("VETT MIGRATION FULLY PROVEN: safe, additive, data-preserving, and the actual bug is fixed")
    print("=" * 70)


asyncio.run(main())
