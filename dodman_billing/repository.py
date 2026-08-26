"""
Data access for the shared `subscriptions` table.

Two deliberate departures from the original schema:

1. Uniqueness is (user_id, product), not user_id alone. The original
   `ON CONFLICT (user_id)` meant a single user could only ever have one
   subscription across every product sharing the database - a second
   product's subscribe would silently overwrite the first's. This wasn't
   hit yet because Vett has been the only product using the table, but it's
   a real landmine for the second one.

2. Row-Level Security scoped by `product`, forced even for the owning role.
   Every connection this repository hands out is pinned to its own
   product via a session-local Postgres setting, and the policy below
   enforces it at the database level - so a bug in any product's webhook
   handler that forgets to filter by product literally cannot read or
   write another product's row, the same guarantee Statement Generator's
   own tenant isolation relies on.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import asyncpg

log = logging.getLogger(__name__)


class SubscriptionRepository:
    def __init__(self, pool: asyncpg.Pool, product: str):
        self._pool = pool
        self.product = product

    @asynccontextmanager
    async def transaction(self):
        """Open a connection pinned to this repository's product, inside a
        transaction. Every write in this module - and every entitlement
        hook the engine calls alongside it - should happen through one of
        these, so a subscription update and its entitlement change commit
        or roll back together."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # set_config(..., is_local=true) scopes to "the rest of the
                # current transaction" - it has to run *inside* the
                # transaction block, not before it, or there's no
                # transaction yet for it to scope to and the setting never
                # takes effect for the statements that follow.
                await conn.execute("SELECT set_config('app.current_product', $1, true)", self.product)
                yield conn

    async def init_schema(self) -> None:
        """Idempotent - safe to call on every app startup, for every product
        sharing this database. Works whether `subscriptions` doesn't exist
        yet (fresh install) or already exists in the pre-multi-product shape
        (a plain `user_id`-only unique index, no `product` column) - the
        second case is a real migration, not just a CREATE TABLE IF NOT
        EXISTS no-op, so it's handled explicitly below rather than assumed
        away.

        Deliberately does NOT enable Row-Level Security while any row still
        has an unmigrated (empty-string) `product` value. Enabling it early
        would make every one of those rows invisible to a normally-scoped
        query (product = '' never matches product = 'vett') from the moment
        RLS turns on until the backfill finishes - a real blackout window
        for live traffic reading its own subscription data, on a table that
        already has real subscribers, not a hypothetical. Backfilling and
        enabling RLS happen together, atomically, in
        migrate_legacy_single_tenant_table() below - never as two steps
        that could be temporally separated in production.
        """
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL,
                    product TEXT NOT NULL DEFAULT '',
                    paystack_customer_id TEXT NOT NULL DEFAULT '',
                    paystack_subscription_id TEXT NOT NULL DEFAULT '',
                    paystack_email_token TEXT NOT NULL DEFAULT '',
                    paystack_authorization_code TEXT NOT NULL DEFAULT '',
                    paystack_auth_reusable BOOLEAN NOT NULL DEFAULT FALSE,
                    paystack_plan_code TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'none',
                    plan TEXT NOT NULL DEFAULT '',
                    current_period_start TIMESTAMPTZ,
                    current_period_end TIMESTAMPTZ,
                    pending_plan TEXT NOT NULL DEFAULT '',
                    pending_plan_code TEXT NOT NULL DEFAULT '',
                    pending_change_at TIMESTAMPTZ,
                    payment_failed_at TIMESTAMPTZ,
                    last_event_data JSONB,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
            # Covers the table already existing without this column - e.g. a
            # single-product table that predates this package. Empty-string
            # default (not a specific product name) because a generic
            # library function has no business guessing which product
            # legacy rows belong to - that's an explicit, one-time decision
            # the product makes via migrate_legacy_single_tenant_table()
            # below, not something that happens silently here.
            await conn.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS product TEXT NOT NULL DEFAULT '';")

            # Named distinctly from any legacy single-column unique index
            # (e.g. Vett's original idx_sub_user) so this never collides
            # with - or silently coexists with and gets overridden by - one
            # that already enforces "one row per user_id" regardless of
            # product. A leftover legacy index like that has to be dropped
            # (see migrate_legacy_single_tenant_table) or a second product
            # can never successfully insert a row for a user who already has
            # one under the first product.
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_user_product ON subscriptions(user_id, product);"
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_product_period "
                                "ON subscriptions(product, current_period_end);")

            has_unmigrated_rows = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM subscriptions WHERE product = '')")
            if has_unmigrated_rows:
                log.warning(
                    "subscriptions has rows with an unmigrated (empty) product value - "
                    "Row-Level Security will NOT be enabled until migrate_legacy_single_tenant_table() "
                    "runs. Reads scoped to a specific product are safe either way (they simply won't "
                    "see the unmigrated rows yet) - this only holds off the RLS policy itself."
                )
                return

            await self._enable_rls(conn)

    async def _enable_rls(self, conn: asyncpg.Connection) -> None:
        await conn.execute("ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;")
        await conn.execute("ALTER TABLE subscriptions FORCE ROW LEVEL SECURITY;")
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename = 'subscriptions' AND policyname = 'product_isolation'
                ) THEN
                    CREATE POLICY product_isolation ON subscriptions
                        USING (product = current_setting('app.current_product', true))
                        WITH CHECK (product = current_setting('app.current_product', true));
                END IF;
            END
            $$;
        """)


    async def migrate_legacy_single_tenant_table(self, *, backfill_product: str,
                                                   legacy_unique_index_name: str,
                                                   dry_run: bool = True) -> dict:
        """One-time, deliberate migration for a `subscriptions` table that
        predates the product column - e.g. Vett's original table, which has
        a unique index on `user_id` alone (`idx_sub_user`). This is NOT
        called by init_schema() and never runs automatically: dropping a
        live unique index and backfilling every existing row's `product`
        value is exactly the kind of change that deserves a human looking at
        the numbers before it commits, not a library doing it silently on
        the next app restart.

        Backfilling and enabling Row-Level Security happen together, in one
        transaction - RLS is never turned on while unmigrated rows could
        still be sitting there invisible to a normally-scoped read. If this
        fails partway, nothing commits; there's no state where some rows are
        backfilled but RLS isn't active yet, or vice versa.

        Call init_schema() first (so the `product` column and its new
        composite index exist), then call this once. With dry_run=True
        (the default) nothing is changed - you get back the row count that
        WOULD be backfilled and whether the legacy index still exists, so
        you can confirm the numbers match what you expect before rerunning
        with dry_run=False.
        """
        async with self._pool.acquire() as conn:
            to_backfill = await conn.fetchval(
                "SELECT COUNT(*) FROM subscriptions WHERE product = ''"
            )
            legacy_index_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = $1)",
                legacy_unique_index_name,
            )

            if dry_run:
                return {
                    "dry_run": True,
                    "rows_to_backfill": to_backfill,
                    "backfill_product": backfill_product,
                    "legacy_index_exists": legacy_index_exists,
                    "legacy_index_name": legacy_unique_index_name,
                }

            async with conn.transaction():
                backfilled = await conn.fetchval(
                    "WITH updated AS (UPDATE subscriptions SET product = $1 WHERE product = '' RETURNING 1) "
                    "SELECT COUNT(*) FROM updated",
                    backfill_product,
                )
                if legacy_index_exists:
                    await conn.execute(f'DROP INDEX IF EXISTS "{legacy_unique_index_name}";')
                # Same transaction as the backfill above - see docstring.
                await self._enable_rls(conn)

            return {
                "dry_run": False,
                "rows_backfilled": backfilled,
                "backfill_product": backfill_product,
                "legacy_index_dropped": bool(legacy_index_exists),
                "rls_enabled": True,
            }


    async def upsert(self, conn: asyncpg.Connection, user_id: str, *, customer_id: str = "",
                      subscription_id: str = "", email_token: str = "", authorization_code: str = "",
                      auth_reusable: bool = False, plan_code: str = "", status: str,
                      plan: str, period_start: datetime | None, period_end: datetime | None,
                      raw_event: dict) -> None:
        """Same 'empty value never overwrites a real one' rule as the
        original _upsert_subscription, for the same reason: a recurring
        charge.success event doesn't repeat every identifier the initial
        subscribe did, and blindly overwriting with blanks was the actual
        cause of a production bug (Paystack's "code is required" error on
        cancel, because paystack_subscription_id had been wiped by a later,
        sparser event)."""
        await conn.execute(
            """
            INSERT INTO subscriptions
                (user_id, product, paystack_customer_id, paystack_subscription_id,
                 paystack_email_token, paystack_authorization_code, paystack_auth_reusable,
                 paystack_plan_code, status, plan, current_period_start, current_period_end,
                 last_event_data, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb, NOW())
            ON CONFLICT (user_id, product) DO UPDATE SET
                paystack_customer_id        = COALESCE(NULLIF(EXCLUDED.paystack_customer_id, ''), subscriptions.paystack_customer_id),
                paystack_subscription_id    = COALESCE(NULLIF(EXCLUDED.paystack_subscription_id, ''), subscriptions.paystack_subscription_id),
                paystack_email_token        = COALESCE(NULLIF(EXCLUDED.paystack_email_token, ''), subscriptions.paystack_email_token),
                paystack_authorization_code = COALESCE(NULLIF(EXCLUDED.paystack_authorization_code, ''), subscriptions.paystack_authorization_code),
                paystack_auth_reusable      = EXCLUDED.paystack_auth_reusable OR subscriptions.paystack_auth_reusable,
                paystack_plan_code          = EXCLUDED.paystack_plan_code,
                status                      = EXCLUDED.status,
                plan                        = EXCLUDED.plan,
                current_period_start        = EXCLUDED.current_period_start,
                current_period_end          = EXCLUDED.current_period_end,
                last_event_data             = EXCLUDED.last_event_data,
                updated_at                  = NOW()
            """,
            user_id, self.product, customer_id, subscription_id, email_token, authorization_code,
            auth_reusable, plan_code, status, plan, period_start, period_end, json.dumps(raw_event),
        )

    async def get(self, conn: asyncpg.Connection, user_id: str) -> asyncpg.Record | None:
        return await conn.fetchrow(
            "SELECT * FROM subscriptions WHERE user_id = $1 AND product = $2",
            user_id, self.product,
        )

    async def clear_payment_failed(self, conn: asyncpg.Connection, user_id: str) -> None:
        await conn.execute(
            "UPDATE subscriptions SET payment_failed_at = NULL WHERE user_id = $1 AND product = $2",
            user_id, self.product,
        )

    async def set_status(self, conn: asyncpg.Connection, user_id: str, status: str) -> None:
        await conn.execute(
            "UPDATE subscriptions SET status = $1, updated_at = NOW() WHERE user_id = $2 AND product = $3",
            status, user_id, self.product,
        )

    async def mark_payment_failed(self, conn: asyncpg.Connection, user_id: str, raw_event: dict) -> None:
        await conn.execute(
            """
            UPDATE subscriptions
            SET status = 'attention', payment_failed_at = COALESCE(payment_failed_at, NOW()),
                last_event_data = $1::jsonb, updated_at = NOW()
            WHERE user_id = $2 AND product = $3
            """,
            json.dumps(raw_event), user_id, self.product,
        )

    async def schedule_pending_change(self, conn: asyncpg.Connection, user_id: str, *,
                                        pending_plan: str, pending_plan_code: str,
                                        pending_change_at: datetime) -> None:
        await conn.execute(
            """
            UPDATE subscriptions
            SET status = 'non-renewing', pending_plan = $1, pending_plan_code = $2,
                pending_change_at = $3, updated_at = NOW()
            WHERE user_id = $4 AND product = $5
            """,
            pending_plan, pending_plan_code, pending_change_at, user_id, self.product,
        )

    async def clear_pending_change(self, conn: asyncpg.Connection, user_id: str, *, status: str | None = None) -> None:
        if status is not None:
            await conn.execute(
                """
                UPDATE subscriptions
                SET pending_plan = '', pending_plan_code = '', pending_change_at = NULL,
                    status = $1, updated_at = NOW()
                WHERE user_id = $2 AND product = $3
                """,
                status, user_id, self.product,
            )
        else:
            await conn.execute(
                """
                UPDATE subscriptions
                SET pending_plan = '', pending_plan_code = '', pending_change_at = NULL, updated_at = NOW()
                WHERE user_id = $1 AND product = $2
                """,
                user_id, self.product,
            )

    async def apply_plan_change(self, conn: asyncpg.Connection, user_id: str, *,
                                  plan: str, plan_code: str, subscription_id: str = "") -> None:
        await conn.execute(
            """
            UPDATE subscriptions
            SET plan = $1, paystack_plan_code = $2, status = 'active',
                paystack_subscription_id = COALESCE(NULLIF($3, ''), paystack_subscription_id),
                pending_plan = '', pending_plan_code = '', pending_change_at = NULL,
                updated_at = NOW()
            WHERE user_id = $4 AND product = $5
            """,
            plan, plan_code, subscription_id, user_id, self.product,
        )

    async def list_due_for_renewal(self, conn: asyncpg.Connection, days: int) -> list[asyncpg.Record]:
        return await conn.fetch(
            """
            SELECT * FROM subscriptions
            WHERE product = $1
              AND status IN ('active', 'non-renewing', 'attention')
              AND current_period_end IS NOT NULL
              AND current_period_end <= NOW() + ($2 || ' days')::interval
            ORDER BY current_period_end ASC
            """,
            self.product, str(days),
        )

    async def list_pending_changes_due(self, conn: asyncpg.Connection) -> list[asyncpg.Record]:
        return await conn.fetch(
            "SELECT * FROM subscriptions WHERE product = $1 AND pending_plan <> '' AND pending_change_at <= NOW()",
            self.product,
        )

    async def list_grace_candidates(self, conn: asyncpg.Connection, grace_hours: int) -> list[asyncpg.Record]:
        return await conn.fetch(
            """
            SELECT * FROM subscriptions
            WHERE product = $1
              AND (
                (status = 'attention' AND payment_failed_at IS NOT NULL
                    AND payment_failed_at <= NOW() - ($2 || ' hours')::interval)
                OR
                (status = 'active' AND current_period_end IS NOT NULL
                    AND current_period_end <= NOW() - INTERVAL '48 hours')
              )
            """,
            self.product, str(grace_hours),
        )
