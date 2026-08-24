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

## Wiring examples

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
