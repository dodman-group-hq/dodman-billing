"""
Configuration a product supplies when wiring up the billing package.

Nothing here is a default guess at your business logic - every field is
something Vett's existing billing router had hardcoded (a plan map, a
product string sprinkled through webhook filters, brand colors baked into
HTML strings) that a *second* product would otherwise have needed to copy
and hand-edit. Centralizing it here is the actual point of the package.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BrandingConfig:
    """Everything that makes an invoice/email look like it's from a specific
    product rather than a generic billing system."""
    product_display_name: str            # e.g. "Vett" or "Statement Generator"
    company_name: str = "Dodman Group"
    support_email: str = ""
    accent_color: str = "#10b981"        # used for amounts/success badges
    brand_color: str = "#f59e0b"         # used for the small brand label
    font_family: str = "'IBM Plex Mono', Courier, monospace"
    bg_color: str = "#07070a"
    card_color: str = "#0f0f12"
    header_color: str = "#16161a"
    border_color: str = "#232329"
    text_color: str = "#d0d0e0"
    muted_color: str = "#6b6b7a"


@dataclass(frozen=True)
class BillingConfig:
    """Core wiring for one product's billing. One BillingConfig + one
    BillingEngine per product, even though they may share the same
    Postgres database and the same Paystack account."""
    product_slug: str                    # e.g. "vett", "statement-generator" - see repository.py for why this matters
    paystack_secret_key: str
    paystack_webhook_secret: str
    app_base_url: str
    branding: BrandingConfig
    resend_api_key: str = ""
    resend_from: str = ""
    payment_failure_grace_hours: int = 72
    paystack_cache_ttl_seconds: int = 300
    currency_default: str = "KES"

    # Where THIS product's own pages live. Every product has a different
    # dashboard URL, and the defaults below are Vett's - so Vett keeps
    # working with no changes - but a product whose pages are elsewhere MUST
    # set them or checkout redirects and every email button point at a URL
    # that doesn't exist.
    #
    # Paths only (no query string): the router appends its own query
    # parameters, e.g. "<checkout_success_path>?billing=success".
    checkout_success_path: str = "/dashboard/billing"      # after a successful checkout (customer sees a confirmation page)
    checkout_failure_path: str = "/dashboard/billing"    # after a failed/abandoned checkout
    dashboard_path: str = "/dashboard/"                  # target of "Go to Dashboard" / "Re-subscribe" email buttons