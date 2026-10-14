"""``EntitlementsGateway`` over core NATS: ``entitlements.plan.set`` (internal request)."""

from mowdeck_contracts.v1 import PlanSetReply, PlanSetRequest
from mowdeck_contracts.v1.subjects import ENTITLEMENTS_PLAN_SET
from mowdeck_service_kit.bus import EventBus

from billing.domain.errors import ProviderError


class BusEntitlementsGateway:
    """Sends ``entitlements.plan.set`` and waits for the reply.

    Without a bus (NATS disabled) or without an answer it raises, so the webhook answers 500
    and the payment provider delivers it again later.
    """

    def __init__(self, bus: EventBus | None, timeout: float = 5.0) -> None:
        self.bus = bus
        self.timeout = timeout

    async def set_plan(self, request: PlanSetRequest) -> PlanSetReply:
        """Send the request; raise when Entitlements does not answer."""
        if self.bus is None:
            raise ProviderError("no event bus: the plan cannot be set in Entitlements")
        reply = await self.bus.request(ENTITLEMENTS_PLAN_SET, request, timeout=self.timeout)
        return reply.model(PlanSetReply)
