"""
One-time migration for Vett's real `subscriptions` table: adds the
`product` column, backfills every existing row to 'vett' (confirmed safe -
Vett is the only product that has ever written to this table), drops the
legacy `idx_sub_user` unique index, and enables Row-Level Security - all
in the same transaction as the backfill, so there's no window where rows
are backfilled but RLS isn't active yet (or the reverse).

Run this ONCE, against your real TENANT_DATABASE_URL, before deploying a
version of Vett that mounts the new dodman_billing router. It's safe to
run against a database that's already been migrated (it's a no-op then),
but it is NOT idempotent about being run twice on legacy data in the sense
of "safe to run casually" - review the dry-run output every time.

Usage:
    python vett_migration_script.py                 # dry run (default) - shows counts, changes nothing
    python vett_migration_script.py --apply          # the real thing - asks for confirmation first

Requires TENANT_DATABASE_URL in the environment (same variable Vett's own
main.py uses).
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import asyncpg
from dodman_billing.repository import SubscriptionRepository

LEGACY_UNIQUE_INDEX_NAME = "idx_sub_user"
BACKFILL_PRODUCT = "vett"


async def main():
    parser = argparse.ArgumentParser(description="Migrate Vett's subscriptions table to the multi-product shape.")
    parser.add_argument("--apply", action="store_true", help="Actually run the migration. Without this flag, it's a dry run.")
    args = parser.parse_args()

    dsn = os.environ.get("TENANT_DATABASE_URL")
    if not dsn:
        print("TENANT_DATABASE_URL is not set. Aborting.")
        sys.exit(1)

    pool = await asyncpg.create_pool(dsn)
    repo = SubscriptionRepository(pool, product=BACKFILL_PRODUCT)

    print("Step 1/2: init_schema() - adds the product column and the new composite index.")
    print("          Does NOT enable RLS yet, since legacy rows still exist at this point.\n")
    await repo.init_schema()

    print("Step 2/2: migrate_legacy_single_tenant_table() - dry run first, always.\n")
    dry_run = await repo.migrate_legacy_single_tenant_table(
        backfill_product=BACKFILL_PRODUCT,
        legacy_unique_index_name=LEGACY_UNIQUE_INDEX_NAME,
        dry_run=True,
    )
    print(f"  Rows that will be backfilled to product='{BACKFILL_PRODUCT}':  {dry_run['rows_to_backfill']}")
    print(f"  Legacy index '{LEGACY_UNIQUE_INDEX_NAME}' present:              {dry_run['legacy_index_exists']}")

    if dry_run["rows_to_backfill"] == 0 and not dry_run["legacy_index_exists"]:
        print("\nNothing to do - this table has already been migrated.")
        await pool.close()
        return

    if not args.apply:
        print("\nDry run only - nothing was changed. Re-run with --apply to actually migrate.")
        await pool.close()
        return

    print(f"\nAbout to backfill {dry_run['rows_to_backfill']} row(s), drop index "
          f"'{LEGACY_UNIQUE_INDEX_NAME}', and enable Row-Level Security on subscriptions.")
    confirm = input("Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted - nothing was changed.")
        await pool.close()
        return

    result = await repo.migrate_legacy_single_tenant_table(
        backfill_product=BACKFILL_PRODUCT,
        legacy_unique_index_name=LEGACY_UNIQUE_INDEX_NAME,
        dry_run=False,
    )
    print(f"\nDone: {result}")
    print("\nRemaining manual step - update main.py's get_current_user (around line 1889):")
    print("  LEFT JOIN subscriptions s ON s.user_id = u.id")
    print("  becomes:")
    print("  LEFT JOIN subscriptions s ON s.user_id = u.id AND s.product = 'vett'")
    print("(Without this, that query could return an arbitrary product's row for a user")
    print(" once a second product starts writing to this table - harmless today since only")
    print(" Vett has any rows, but it stops being harmless the moment that changes.)")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
