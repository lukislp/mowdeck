"""``PlanCatalog`` over HTTP: the public ``GET /api/v1/entitlements/plans`` of Entitlements.

Plans are data in Entitlements (ADR 0028); Billing only adds prices. Names, descriptions and
trial days are read from there (cached by the caller through the price cache of the status).
"""

from typing import Any

import httpx

from billing.domain.models import OfferedPlan

PLANS_PATH = "/api/v1/entitlements/plans"


class HttpPlanCatalog:
    """Reads the offered plans from Entitlements (``MOWDECK_BILLING_ENTITLEMENTS_URL``)."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    async def offered(self) -> list[OfferedPlan]:
        """The active plans; raises on any error (the caller degrades gracefully)."""
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}{PLANS_PATH}")
            response.raise_for_status()
            data: Any = response.json()
        if not isinstance(data, list):
            msg = "unexpected plans answer"
            raise ValueError(msg)
        return [
            OfferedPlan(
                key=str(item["key"]),
                name=str(item.get("name") or item["key"]),
                description=item.get("description"),
                trial_days=int(item.get("trial_days") or 0),
                features=[str(f) for f in item.get("features") or []],
            )
            for item in data
            if isinstance(item, dict) and item.get("active", True)
        ]


class StaticPlanCatalog:
    """A fixed catalogue (tests, the OpenAPI export, no Entitlements URL configured)."""

    def __init__(self, plans: list[OfferedPlan] | None = None) -> None:
        self.plans = plans or []

    async def offered(self) -> list[OfferedPlan]:
        """The configured plans."""
        return list(self.plans)
