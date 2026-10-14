"""Path preview requests: ``gateway.mower.*.path.preview``, core NATS.

Fleet asks the gateway that holds the mower to let it plan the route of a start without starting
(``PathPreviewGatewayRequest`` in, ``PathPreviewReply`` out). Every gateway instance subscribes
(no queue group); only the instance whose shard holds the mower answers, the others stay silent,
so Fleet gets exactly one reply or a timeout. Each request is answered in its own task: planning
takes up to ``path_preview_timeout_seconds`` and must not hold up the subscription.
"""

import asyncio

from mowdeck_contracts.v1 import PathPreviewGatewayRequest
from mowdeck_contracts.v1.subjects import ALL_PATH_PREVIEWS, parse_path_preview_subject
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, Subscription
from pydantic import ValidationError

from device_gateway.application.gateway import DeviceGateway

_log = get_logger(__name__)


class PathPreviewResponder:
    """Answers path preview requests for the mowers of this shard."""

    def __init__(self, gateway: DeviceGateway, *, timeout_seconds: float) -> None:
        self._gateway = gateway
        self._timeout_seconds = timeout_seconds
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(self, message: BusMessage) -> None:
        """Decode and check the request, then answer it in the background."""
        try:
            request = message.model(PathPreviewGatewayRequest)
        except ValidationError as exc:
            raise DropMessage(f"invalid path preview request: {exc.error_count()} errors") from exc
        try:
            subject_mower = parse_path_preview_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if subject_mower != request.mower_id:
            raise DropMessage("subject does not match the request's mower")
        task = asyncio.create_task(self._answer(message, request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, message: BusMessage, request: PathPreviewGatewayRequest) -> None:
        reply = await self._gateway.path_preview(request, timeout_seconds=self._timeout_seconds)
        if reply is None:
            return  # another shard holds the mower
        try:
            await message.respond(reply)
        except Exception as exc:
            _log.warning(
                "path preview reply failed", mower_id=str(request.mower_id), error=repr(exc)
            )
            return
        _log.info(
            "path preview answered",
            mower_id=str(request.mower_id),
            subject=str(request.requested_by.subject),
            outcome="path" if reply.error is None else reply.error.code.value,
        )


async def subscribe_path_previews(
    bus: EventBus, gateway: DeviceGateway, *, timeout_seconds: float
) -> Subscription:
    """Subscribe to ``gateway.mower.*.path.preview`` on every instance."""
    responder = PathPreviewResponder(gateway, timeout_seconds=timeout_seconds)
    return await bus.subscribe(ALL_PATH_PREVIEWS, responder.handle)
