"""
Worked example: how Vett wires up dodman_billing, using its real plan
structure (starter/pro) and its existing entitlement model (a tier column
on api_keys). This is meant to be read side-by-side with your current
billing_router.py to see what moves where - it is not a drop-in replacement
file, since Vett's actual app also needs get_pool/get_current_user/
verify_internal_secret/_write_audit_log wired from its own app module.
"""
import os
import asyncpg

from dodman_billing import (
    BillingConfig, BrandingConfig, Plan, PlanRegistry,
    SubscriptionRepository, PaystackClient, SharedUsersResolver,
    BillingEngine, build_billing_router,
)
from dodman_billing.notifications import ResendEmailSender


class VettEntitlementHooks:
    """Replaces the original UPDATE api_keys SET tier = ... calls that were
    scattered across the router - now there's exactly one place this logic
    lives, called at exactly the moments it should run."""

    async def on_activated(self, conn: asyncpg.Connection, user_id: str, plan_key: str) -> None:
        await conn.execute(
            "UPDATE api_keys SET tier = $1 WHERE user_id = $2 AND environment = 'live' AND is_active = TRUE",
            plan_key, user_id,
        )

    async def on_cancelled(self, conn: asyncpg.Connection, user_id: str) -> None:
        await conn.execute(
            "UPDATE api_keys SET is_active = FALSE WHERE user_id = $1 AND environment = 'live' AND is_active = TRUE",
            user_id,
        )


async def build_vett_billing_router(pool: asyncpg.Pool, *, get_current_user, verify_internal_secret, write_audit_log):
    config = BillingConfig(
        product_slug="vett",
        paystack_secret_key=os.environ["PAYSTACK_SECRET_KEY"],
        paystack_webhook_secret=os.environ["PAYSTACK_WEBHOOK_SECRET"],
        app_base_url=os.environ["APP_BASE_URL"],
        branding=BrandingConfig(
            product_display_name="Vett",
            support_email=os.getenv("RESEND_FROM", ""),
        ),
        resend_api_key=os.getenv("RESEND_API_KEY", ""),
        resend_from=os.getenv("RESEND_FROM", ""),
    )

    plans = PlanRegistry([
        Plan(key="starter", paystack_plan_code=os.environ["PAYSTACK_PLAN_STARTER"], rank=1),
        Plan(key="pro", paystack_plan_code=os.environ["PAYSTACK_PLAN_PRO"], rank=2),
    ])

    repository = SubscriptionRepository(pool, product=config.product_slug)
    await repository.init_schema()  # safe to call every startup

    resolver = SharedUsersResolver(pool)  # the shared `users` table

    engine = BillingEngine(
        config=config,
        plans=plans,
        repository=repository,
        paystack=PaystackClient(config.paystack_secret_key),
        hooks=VettEntitlementHooks(),
        resolve_user_by_email=resolver.user_id_by_email,
        resolve_email_by_user_id=resolver.email_by_user_id,
        email_sender=ResendEmailSender(config.resend_api_key, config.resend_from) if config.resend_api_key else None,
    )

    return build_billing_router(
        engine=engine,
        get_current_user=get_current_user,
        verify_internal_secret=verify_internal_secret,
        audit_log=write_audit_log,  # your existing _write_audit_log, same signature
    )


# In your reconciliation cron/scheduled task (was two module-level functions
# in the original file - now just two engine method calls):
#
#     await engine.process_due_plan_changes()
#     await engine.enforce_payment_grace_periods()
