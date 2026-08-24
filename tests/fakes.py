"""Fakes for testing BillingEngine without hitting the real Paystack API."""
import uuid


class FakePaystackClient:
    """Simulates just enough of Paystack's behavior to drive the engine
    through real state transitions. Not a mock of specific calls - an
    actual (simplified) stateful implementation, so the engine's logic is
    exercised for real, not just asserted to have "called the right thing"."""

    def __init__(self):
        self.plans = {}          # plan_code -> {"amount": int, "interval": "monthly"}
        self.transactions = {}   # reference -> tx dict
        self.subscriptions = {}  # subscription_code -> sub dict
        self.disabled = set()
        self.next_ref_counter = 0

    def seed_plan(self, plan_code: str, amount: int, interval: str = "monthly"):
        self.plans[plan_code] = {"amount": amount, "interval": interval}

    def _new_ref(self) -> str:
        self.next_ref_counter += 1
        return f"fake_ref_{self.next_ref_counter}"

    async def initialize_transaction(self, *, email, amount, plan_code, callback_url, metadata):
        ref = self._new_ref()
        self.transactions[ref] = {
            "status": "pending", "reference": ref, "amount": amount, "currency": "KES",
            "customer": {"id": abs(hash(email)) % 100000, "email": email},
            "plan": {"plan_code": plan_code}, "metadata": metadata,
        }
        return {"data": {"authorization_url": f"https://paystack.test/pay/{ref}", "access_code": ref, "reference": ref}}

    def mark_paid(self, reference: str, *, subscription_code: str | None = None, email_token: str = "tok_abc",
                  authorization_code: str = "auth_abc", reusable: bool = True):
        """Test helper - simulates the customer actually completing payment on Paystack's hosted page."""
        tx = self.transactions[reference]
        tx["status"] = "success"
        tx["paid_at"] = "2026-01-01T00:00:00.000Z"
        sub_code = subscription_code or f"SUB_{uuid.uuid4().hex[:8]}"
        tx["subscription"] = {"subscription_code": sub_code, "email_token": email_token,
                               "createdAt": "2026-01-01T00:00:00.000Z", "next_payment_date": "2026-02-01T00:00:00.000Z"}
        tx["authorization"] = {"authorization_code": authorization_code, "reusable": reusable}
        self.subscriptions[sub_code] = {"subscription_code": sub_code, "status": "active", "email_token": email_token,
                                          "customer": tx["customer"], "plan": tx["plan"],
                                          "next_payment_date": "2026-02-01T00:00:00.000Z"}
        return sub_code

    async def verify_transaction(self, reference: str) -> dict:
        return {"data": self.transactions[reference]}

    async def get_plan(self, plan_code: str) -> dict:
        return {"data": self.plans.get(plan_code, {"amount": 0})}

    async def charge_authorization(self, *, authorization_code, email, amount, plan_code, metadata):
        ref = self._new_ref()
        sub_code = f"SUB_{uuid.uuid4().hex[:8]}"
        self.subscriptions[sub_code] = {"subscription_code": sub_code, "status": "active",
                                          "email_token": "tok_abc", "next_payment_date": "2026-03-01T00:00:00.000Z"}
        return {"data": {"status": "success", "reference": ref, "subscription": {"subscription_code": sub_code}}}

    async def disable_subscription(self, *, code, token):
        self.disabled.add(code)
        if code in self.subscriptions:
            self.subscriptions[code]["status"] = "non-renewing"
        return {"data": {"status": True}}

    async def enable_subscription(self, *, code, token):
        self.disabled.discard(code)
        if code in self.subscriptions:
            self.subscriptions[code]["status"] = "active"
        return {"data": {"status": True}}

    async def get_subscription(self, subscription_id: str) -> dict:
        return {"data": self.subscriptions.get(subscription_id, {})}

    async def list_subscriptions_for_customer(self, customer_id: str) -> list[dict]:
        return [s for s in self.subscriptions.values() if str(s.get("customer", {}).get("id")) == str(customer_id)]

    async def list_transactions(self, *, email=None, page=1, per_page=20, status="success"):
        rows = [tx for tx in self.transactions.values()
                if tx["status"] == status and (not email or tx["customer"]["email"] == email)]
        return {"data": rows, "meta": {"total": len(rows), "pageCount": 1}}


class RecordingHooks:
    """Stub EntitlementHooks that just records what it was told, so tests
    can assert the engine called it with the right plan at the right time."""
    def __init__(self):
        self.activated: list[tuple[str, str]] = []   # (user_id, plan_key)
        self.cancelled: list[str] = []

    async def on_activated(self, conn, user_id, plan_key):
        self.activated.append((str(user_id), plan_key))

    async def on_cancelled(self, conn, user_id):
        self.cancelled.append(str(user_id))
