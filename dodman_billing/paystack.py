"""
Thin async wrapper around the Paystack REST API. Nothing product-specific
lives here - it's the same _paystack_post/_paystack_get pattern from the
original router, generalized into every endpoint the billing engine needs,
plus a Protocol so tests can swap in a fake without touching the engine.
"""
from typing import Protocol, Any
import httpx


class PaystackClientProtocol(Protocol):
    async def initialize_transaction(self, *, email: str, amount: int, plan_code: str,
                                       callback_url: str, metadata: dict) -> dict: ...
    async def verify_transaction(self, reference: str) -> dict: ...
    async def get_plan(self, plan_code: str) -> dict: ...
    async def charge_authorization(self, *, authorization_code: str, email: str, amount: int,
                                     plan_code: str, metadata: dict) -> dict: ...
    async def disable_subscription(self, *, code: str, token: str) -> dict: ...
    async def enable_subscription(self, *, code: str, token: str) -> dict: ...
    async def get_subscription(self, subscription_id: str) -> dict: ...
    async def list_subscriptions_for_customer(self, customer_id: str) -> list[dict]: ...
    async def list_transactions(self, *, email: str | None = None, page: int = 1,
                                  per_page: int = 20, status: str = "success") -> dict: ...


class PaystackClient:
    """Real implementation, talking to https://api.paystack.co."""

    BASE_URL = "https://api.paystack.co"

    def __init__(self, secret_key: str, timeout: float = 20.0):
        self._secret_key = secret_key
        self._timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._secret_key}", "Content-Type": "application/json"}

    async def _post(self, endpoint: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self.BASE_URL}{endpoint}", json=payload, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def _get(self, endpoint: str) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self.BASE_URL}{endpoint}", headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def initialize_transaction(self, *, email: str, amount: int, plan_code: str,
                                       callback_url: str, metadata: dict) -> dict:
        return await self._post("/transaction/initialize", {
            "email": email, "amount": amount, "plan": plan_code,
            "callback_url": callback_url, "metadata": metadata,
        })

    async def verify_transaction(self, reference: str) -> dict:
        return await self._get(f"/transaction/verify/{reference}")

    async def get_plan(self, plan_code: str) -> dict:
        return await self._get(f"/plan/{plan_code}")

    async def charge_authorization(self, *, authorization_code: str, email: str, amount: int,
                                     plan_code: str, metadata: dict) -> dict:
        return await self._post("/transaction/charge_authorization", {
            "authorization_code": authorization_code, "email": email,
            "amount": amount, "plan": plan_code, "metadata": metadata,
        })

    async def disable_subscription(self, *, code: str, token: str) -> dict:
        return await self._post("/subscription/disable", {"code": code, "token": token})

    async def enable_subscription(self, *, code: str, token: str) -> dict:
        return await self._post("/subscription/enable", {"code": code, "token": token})

    async def get_subscription(self, subscription_id: str) -> dict:
        return await self._get(f"/subscription/{subscription_id}")

    async def list_subscriptions_for_customer(self, customer_id: str) -> list[dict]:
        data = await self._get(f"/subscription?customer={customer_id}")
        return data.get("data", []) or []

    async def list_transactions(self, *, email: str | None = None, page: int = 1,
                                  per_page: int = 20, status: str = "success") -> dict:
        endpoint = f"/transaction?page={page}&perPage={min(per_page, 100)}&status={status}"
        if email:
            endpoint += f"&email={email}"
        return await self._get(endpoint)
