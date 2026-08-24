"""
The one piece of billing that can never be generic: what does "this user is
now on the Pro plan" actually *mean* for your product?

Vett's answer is `UPDATE api_keys SET tier = 'pro' WHERE user_id = ...`.
Statement Generator's answer is updating four different columns
(monthly_job_limit, max_rows_per_job, password_protection_allowed, plan) on
a totally differently-shaped table. There's no way to write that once and
reuse it - so instead every product implements this small interface and
hands it to BillingEngine. The engine calls it at exactly the moments
entitlements should change; it never touches your product's own tables
itself.

Both methods receive the *same* asyncpg connection/transaction the engine
used to write the subscription row - so if your entitlement update fails,
the whole thing (subscription state + entitlement) rolls back together
rather than leaving them disagreeing with each other, which is a real gap
in the original code (the api_keys update was a separate, later
transaction that could fail independently of the subscription upsert).
"""
from typing import Protocol
import asyncpg


class EntitlementHooks(Protocol):
    async def on_activated(self, conn: asyncpg.Connection, user_id: str, plan_key: str) -> None:
        """Called whenever a user should have active access at plan_key -
        first subscribe, a renewal, an immediate plan change, a scheduled
        downgrade taking effect, or a manual reactivation."""
        ...

    async def on_cancelled(self, conn: asyncpg.Connection, user_id: str) -> None:
        """Called whenever access should be revoked - explicit cancellation
        reaching period end, or the grace-period reconciliation job giving
        up on a failed/missing payment."""
        ...
