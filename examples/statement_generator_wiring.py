"""
Worked example for Statement Generator - NOT usable as-is yet, since
Statement Generator is currently Flask + psycopg2 (sync) and this package
is async-native (FastAPI + asyncpg). This shows the shape the wiring takes
once that migration happens (the agreed next step after this package).

The interesting part here isn't the framework glue - it's that
StatementGeneratorEntitlementHooks maps a subscription plan onto FOUR
different columns instead of Vett's one `tier` string. That's exactly what
EntitlementHooks exists for: two totally different entitlement shapes,
zero shared code needed to support either one.
"""
import os
import asyncpg

from dodman_billing import (
    BillingConfig, BrandingConfig, Plan, PlanRegistry,
    SubscriptionRepository, PaystackClient, SharedUsersResolver,
    BillingEngine, build_billing_router,
)
from dodman_billing.notifications import ResendEmailSender

# Mirrors the plan shape already live in Statement Generator's own
# statement_api_clients table (see scripts/create_api_key.py FREE_DEFAULTS /
# OTHER_DEFAULTS) - the billing package doesn't need to know these numbers,
# only the entitlement hook does.
PLAN_ENTITLEMENTS = {
    "free": {"monthly_job_limit": 5, "max_rows_per_job": 25, "password_protection_allowed": False},
    "paid": {"monthly_job_limit": None, "max_rows_per_job": None, "password_protection_allowed": True},
}


class StatementGeneratorEntitlementHooks:
    async def on_activated(self, conn: asyncpg.Connection, user_id: str, plan_key: str) -> None:
        entitlements = PLAN_ENTITLEMENTS[plan_key]
        await conn.execute(
            """
            UPDATE statement_api_clients
            SET plan = $1, monthly_job_limit = $2, max_rows_per_job = $3,
                password_protection_allowed = $4, is_active = TRUE
            WHERE user_id = $5
            """,
            plan_key, entitlements["monthly_job_limit"], entitlements["max_rows_per_job"],
            entitlements["password_protection_allowed"], user_id,
        )

    async def on_cancelled(self, conn: asyncpg.Connection, user_id: str) -> None:
        await conn.execute("UPDATE statement_api_clients SET is_active = FALSE WHERE user_id = $1", user_id)


async def build_statement_generator_billing_router(pool: asyncpg.Pool, *, get_current_user, verify_internal_secret):
    config = BillingConfig(
        product_slug="statement-generator",
        paystack_secret_key=os.environ["PAYSTACK_SECRET_KEY"],       # same Paystack account as Vett is fine
        paystack_webhook_secret=os.environ["PAYSTACK_WEBHOOK_SECRET"],
        app_base_url=os.environ["APP_BASE_URL"],
        branding=BrandingConfig(product_display_name="Statement Generator"),
        resend_api_key=os.getenv("RESEND_API_KEY", ""),
        resend_from=os.getenv("RESEND_FROM", ""),
    )

    plans = PlanRegistry([
        Plan(key="free", paystack_plan_code=os.environ["PAYSTACK_PLAN_SG_FREE"], rank=1),
        Plan(key="paid", paystack_plan_code=os.environ["PAYSTACK_PLAN_SG_PAID"], rank=2),
    ])

    repository = SubscriptionRepository(pool, product=config.product_slug)
    await repository.init_schema()  # same shared subscriptions table Vett uses, own product row per user

    resolver = SharedUsersResolver(pool)  # the SAME shared `users` table Vett uses

    engine = BillingEngine(
        config=config, plans=plans, repository=repository,
        paystack=PaystackClient(config.paystack_secret_key),
        hooks=StatementGeneratorEntitlementHooks(),
        resolve_user_by_email=resolver.user_id_by_email,
        resolve_email_by_user_id=resolver.email_by_user_id,
        email_sender=ResendEmailSender(config.resend_api_key, config.resend_from) if config.resend_api_key else None,
    )

    return build_billing_router(engine=engine, get_current_user=get_current_user, verify_internal_secret=verify_internal_secret)
