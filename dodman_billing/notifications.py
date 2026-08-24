"""
Transactional email delivery (via Resend) and the period-start/period-end
resolution logic from the original _resolve_period_dates. Both are pure
infrastructure - no product-specific branching lives here.
"""
from typing import Protocol
import datetime
import logging
import httpx

log = logging.getLogger(__name__)


class EmailSender(Protocol):
    async def send(self, to: str, subject: str, html: str) -> bool: ...


class ResendEmailSender:
    API_URL = "https://api.resend.com/emails"

    def __init__(self, api_key: str, from_addr: str):
        self._api_key = api_key
        self._from = from_addr

    async def send(self, to: str, subject: str, html: str) -> bool:
        if not self._api_key:
            log.warning("No Resend API key configured - email not sent to %s", to)
            return False
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    self.API_URL,
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json={"from": self._from, "to": [to], "subject": subject, "html": html},
                )
                resp.raise_for_status()
                return True
        except Exception:
            log.exception("Resend email failed: to=%s subject=%r", to, subject)
            return False


class NullEmailSender:
    """Used when a product hasn't configured email yet - never raises, just
    logs, so billing logic keeps working end-to-end without Resend."""
    async def send(self, to: str, subject: str, html: str) -> bool:
        log.info("Email sending disabled - would have sent %r to %s", subject, to)
        return False


def resolve_period_dates(tx: dict, sub: dict, plan: dict) -> tuple[str, str]:
    """(period_start, period_end) as ISO strings, best-effort from whatever
    the Paystack transaction/subscription/plan objects actually contain."""
    ps = sub.get("createdAt") or sub.get("created_at") or ""
    pe = sub.get("next_payment_date") or sub.get("nextPaymentDate") or ""
    if ps and pe:
        return ps, pe

    paid_at_raw = tx.get("paid_at") or tx.get("paidAt") or ""
    if paid_at_raw:
        try:
            start_dt = datetime.datetime.fromisoformat(paid_at_raw.replace("Z", "+00:00"))
            ps = start_dt.isoformat()
            interval = (plan.get("interval") or "monthly").lower()
            if interval in ("annually", "yearly"):
                end_dt = start_dt.replace(year=start_dt.year + 1)
            elif interval == "quarterly":
                end_dt = start_dt + datetime.timedelta(days=91)
            elif interval == "weekly":
                end_dt = start_dt + datetime.timedelta(weeks=1)
            else:
                month = start_dt.month + 1
                year = start_dt.year + (month - 1) // 12
                month = ((month - 1) % 12) + 1
                try:
                    end_dt = start_dt.replace(year=year, month=month)
                except ValueError:
                    import calendar
                    last_day = calendar.monthrange(year, month)[1]
                    end_dt = start_dt.replace(year=year, month=month, day=last_day)
            pe = end_dt.isoformat()
        except Exception:
            pass
    return ps, pe


def parse_dt(val: str | None) -> datetime.datetime | None:
    if not val:
        return None
    try:
        return datetime.datetime.fromisoformat(val.replace("Z", "+00:00"))
    except Exception:
        return None
