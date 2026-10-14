"""The ``FleetGateway`` port over core NATS request/reply (``fleet.targets.resolve``,
``fleet.commands.issue``)."""

from uuid import UUID

from mowdeck_contracts.v1 import (
    IssueCommandReply,
    IssueCommandRequest,
    ResolveTargetReply,
    ResolveTargetRequest,
)
from mowdeck_contracts.v1.subjects import FLEET_COMMANDS_ISSUE, FLEET_TARGETS_RESOLVE
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import EventBus
from pydantic import BaseModel, ValidationError

from scheduler.domain.errors import Unavailable

_log = get_logger("scheduler.fleet")


class BusFleetGateway:
    """Asks Fleet; every failure (no bus, no responder, timeout, garbage) is ``Unavailable``."""

    def __init__(self, bus: EventBus | None, timeout_seconds: float = 5.0) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds

    async def _request[T: BaseModel](self, subject: str, payload: BaseModel, reply: type[T]) -> T:
        if self.bus is None or not self.bus.is_connected:
            raise Unavailable("fleet is not reachable: the event bus is not connected")
        try:
            message = await self.bus.request(subject, payload, self.timeout_seconds)
        except Exception as exc:
            _log.warning("fleet did not answer", subject=subject, error=repr(exc))
            raise Unavailable(f"fleet did not answer {subject}") from exc
        try:
            return message.model(reply)
        except ValidationError as exc:
            _log.warning("fleet answered with an invalid reply", subject=subject)
            raise Unavailable("fleet answered with an invalid reply") from exc

    async def resolve(
        self, account_id: UUID, target_type: str, target_id: UUID
    ) -> ResolveTargetReply:
        """``fleet.targets.resolve``."""
        request = ResolveTargetRequest.model_validate(
            {"account_id": account_id, "target_type": target_type, "target_id": target_id}
        )
        return await self._request(FLEET_TARGETS_RESOLVE, request, ResolveTargetReply)

    async def issue(self, request: IssueCommandRequest) -> IssueCommandReply:
        """``fleet.commands.issue``."""
        return await self._request(FLEET_COMMANDS_ISSUE, request, IssueCommandReply)
