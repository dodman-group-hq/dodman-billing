# dodman-billing

Shared Paystack subscription billing for every Dodman Group product on
FastAPI + asyncpg. Built by extracting Vett's working billing router into a
framework-agnostic core with five things each product supplies:

1. **A `PlanRegistry`** - your plan keys, Paystack plan codes, and ranks
2. **`EntitlementHooks`** - what "this user is on Plan X" means for *your*
   tables (Vett updates `api_keys.tier`; a future product might update
   something else entirely)
3. **`BrandingConfig`** - your product name/colors for invoices and emails
4. **A `product_slug`** - so multiple products can share one Postgres
   database and one Paystack account without their data colliding
5. **Identity resolution** - if you use the shared `users` table (see
   below), `SharedUsersResolver` covers this for free

## Two real fixes made along the way, not just an extraction

**The `subscriptions` table's uniqueness was `user_id` alone.** That means
under the original schema, a single person could only ever have one
subscription across *every* product sharing the database - a second
product's subscribe would silently overwrite the first's. This wasn't hit
yet because Vett has been the only consumer. Fixed: uniqueness is now
`(user_id, product)`, tested directly (see `tests/test_engine_lifecycle.py`)
by subscribing the same user to two different products and confirming both
rows survive independently.

**Product-scoping was inconsistent across event types.** The original code
only filtered `charge.success` events by `metadata.product`; every other
event type (`subscription.create`, `subscription.disable`,
`invoice.payment_failed`) trusted whatever user an email happened to
resolve to. Since all products on one Paystack account receive every other
product's webhooks (Paystack allows one webhook URL per account), this was
a latent cross-product risk. Fixed with two layers: consistent metadata
checking across all event types, *and* Row-Level Security on `subscriptions`
scoped by product - forced even for the owning role - so a bug in any
product's handler cannot read or write another product's row, full stop.
Proven in `tests/test_engine_lifecycle.py` by delivering a webhook tagged
for `"vett"` directly to the `"statement-generator"` engine and confirming
it's ignored.

## What's tested (not just written)

- `tests/test_engine_lifecycle.py` - subscribe -> callback -> activation,
  two products/one user with no collision, cross-product webhook rejection,
  cancel -> `subscription.disable` webhook -> entitlement revocation
- `tests/test_plan_changes_and_reconciliation.py` - immediate upgrade with
  proration and a real charge, scheduled downgrade deferred then applied by
  the reconciliation sweep, grace-period revocation, and the "don't revoke
  if Paystack still says active" safety net in both directions
- `tests/test_router_smoke.py` - the FastAPI adapter over real ASGI
  requests: subscribe, checkout callback redirect, portal status, a bad
  plan coming back as a clean 422 instead of a 500, cancel, admin route
  validation

All three run against a real local Postgres (not mocks) using
`tests/fakes.py`'s `FakePaystackClient` - a small stateful simulation of
Paystack, not a call-count mock, so the engine's actual logic is exercised.

Run them: `python3 tests/test_engine_lifecycle.py` (and the other two) with
`DATABASE_URL`/local Postgres available - they don't need real Paystack or
Resend credentials.

## What's covered vs. deferred

**Covered:** subscribe, checkout callback, webhook handling (charge
success, subscription create/disable/not-renew, payment failure),
cancel, reactivate, upgrade/downgrade with proration, scheduled downgrades,
manual transaction verify, invoice list/download/email, both
reconciliation jobs.

**Not yet ported** (present in the original router, not yet in the
package - straightforward to add when a product actually needs them,
deliberately deferred rather than built speculatively):
- `GET /billing/admin/invoices` (admin, all users across all products)
- `GET /billing/transactions` (raw transaction history incl. failed/abandoned)
- `GET /billing/due` (admin, subscriptions due soon)
- `GET /billing/subscription` (live Paystack re-sync with a TTL cache)

## The `users` table

This package assumes products share one `users` table (id, email) rather
than each maintaining its own - see `SharedUsersResolver`. If a product's
identity model doesn't fit that shape, `resolve_user_by_email` and
`resolve_email_by_user_id` on `BillingEngine` are just plain async
callables - swap in your own.

## Tried against Vett's real code (not a guess)

You uploaded the full `main.py` (6,353 lines). Reading it changed real things:

**What matched my earlier stand-ins closely enough to use as-is:** `_error_envelope(code, message, request_id, extra=None)`, `_client_ip(request)`, `verify_internal_secret`, `get_current_user`'s return shape.

**A real bug in the package itself, found only by testing against your actual schema:** `subscriptions` already exists in production with `CREATE UNIQUE INDEX idx_sub_user ON subscriptions (user_id)` - a real, named, live index. My original `init_schema()` only knew how to create a table from scratch; against your existing one, `CREATE TABLE IF NOT EXISTS` silently no-ops, and the very next statement (`ALTER TABLE ... ENABLE ROW LEVEL SECURITY` referencing a `product` column that doesn't exist yet) would have crashed outright on first deploy.

**A more serious design flaw, found by testing the migration itself, not just describing it:** my first fix added the `product` column and enabled RLS in the same automatic `init_schema()` call. That creates a real blackout window - the instant RLS turns on, every existing row (still `product=''`, not yet backfilled) becomes invisible to a `product='vett'`-scoped read, meaning **every existing paying subscriber would appear to have no active subscription** until the separate backfill step ran. Fixed by splitting this properly: `init_schema()` now detects unmigrated rows and refuses to enable RLS until they're gone; the backfill and RLS activation happen together, atomically, in `migrate_legacy_single_tenant_table()` - never as two steps that could be separated in time.

**Proven against your exact legacy schema, not a clean-room assumption:** `tests/test_vett_migration.py` builds a database from your `subscriptions` `CREATE TABLE` statement verbatim (UUID PK, `idx_sub_user`, the works), seeds it with realistic existing subscribers, and confirms: `init_schema()` is purely additive and holds off on RLS correctly; a dry run changes nothing; the real migration backfills every row, drops the legacy index, and enables RLS as one atomic step; all existing subscriber data survives intact; and - the actual point - a second product can now successfully subscribe a user who already has a Vett subscription, which would have raised a unique-violation error before.

**A runnable migration script**, not just library calls: `examples/vett_migration_script.py`. Dry-run by default, requires a typed `yes` confirmation for the real run, reports exactly what changed. I ran it for real against the seeded legacy database (included above) and it worked end-to-end.

**One change that has to happen in your `main.py`, not this package:** `get_current_user` (around line 1889) does its own direct `LEFT JOIN subscriptions s ON s.user_id = u.id`, independent of the billing router entirely. Once a second product's rows can exist for the same user, this needs `AND s.product = 'vett'` or it could return an arbitrary product's row into a Vett session. Harmless today (only Vett has rows), stops being harmless the moment that changes - the migration script prints this reminder at the end of a real run.

**Another structural change, also in `main.py`:** your real `billing_router = APIRouter(prefix="/billing", tags=["billing"])` (line 2270) is built at *module import time*, with routes using `Depends(get_pool)` per-request. This package's `build_billing_router()` needs a fully-constructed `engine` (which needs a live `pool`) up front, so router construction has to move into your `@app.on_event("startup")` handler - after `_pool = await asyncpg.create_pool(...)` runs (around line 800) - rather than happening at import time. That's the one piece of `app.py`'s structure that needs to change, not just a file swap.



- `examples/vett_wiring.py` - Vett's actual plan structure and entitlement
  model, side-by-side with what the original router did
- `examples/statement_generator_wiring.py` - the shape Statement
  Generator's wiring takes once it migrates to FastAPI/asyncpg (not usable
  today - SG is currently Flask/psycopg2, sync)

## Install

Not published anywhere yet - for now:

```
pip install git+https://github.com/Dodman101/dodman-billing.git
```

(once you push this to a repo - or `pip install -e .` from a local clone
for development).
