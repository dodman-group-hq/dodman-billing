"""
A product's plan catalogue. Replaces the hardcoded _PLAN_MAP / _PLAN_RANK /
_plan_name_from_code trio from the original billing router - those three
things always need to agree with each other (a plan code, a rank for
upgrade/downgrade comparison, and a reverse lookup), so bundling them into
one object removes a whole class of "I updated the map but forgot the rank"
bugs.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    key: str            # your internal name, e.g. "starter", "pro"
    paystack_plan_code: str
    rank: int            # higher = more privileged; used to tell upgrade from downgrade


class PlanRegistry:
    def __init__(self, plans: list[Plan]):
        if not plans:
            raise ValueError("PlanRegistry needs at least one plan.")
        by_key = {}
        by_code = {}
        for plan in plans:
            if plan.key in by_key:
                raise ValueError(f"Duplicate plan key: {plan.key!r}")
            by_key[plan.key] = plan
            by_code[plan.paystack_plan_code] = plan
        self._by_key = by_key
        self._by_code = by_code

    def get(self, key: str) -> Plan | None:
        return self._by_key.get(key)

    def get_by_code(self, paystack_plan_code: str) -> Plan | None:
        return self._by_code.get(paystack_plan_code)

    def key_from_code(self, paystack_plan_code: str) -> str:
        """Mirrors the original _plan_name_from_code - falls back to the
        lowest-ranked plan if the code is unrecognized, same as the original
        defaulted to 'starter'."""
        plan = self.get_by_code(paystack_plan_code)
        if plan:
            return plan.key
        return min(self._by_key.values(), key=lambda p: p.rank).key

    def is_upgrade(self, from_key: str, to_key: str) -> bool:
        from_rank = self._by_key[from_key].rank if from_key in self._by_key else 0
        to_plan = self._by_key.get(to_key)
        if to_plan is None:
            raise ValueError(f"Unknown plan key: {to_key!r}")
        return to_plan.rank > from_rank

    def __contains__(self, key: str) -> bool:
        return key in self._by_key

    def keys(self):
        return list(self._by_key.keys())
