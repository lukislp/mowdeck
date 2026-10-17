"""Request handlers of the relay (core NATS request/reply, queue group ``camera-relay``).

* ``relay.camera.attach``: ``RelayAttachRequest`` in, ``RelayAttachReply`` out.
* ``relay.camera.renew``: ``RelayRenewRequest`` in, ``RelayRenewReply`` out.
* ``relay.camera.detach``: ``RelayDetachRequest`` in, ``RelayDetachReply`` out.

The subjects live below ``relay.``, outside every stream; only services reach core NATS, and
the sender (Fleet, later Vision) has authorized the principal already. A request that does not
validate is dropped without a reply (the sender times out), like other internal requests.
"""

from collections.abc import Awaitable, Callable

from mowdeck_contracts.v1.relay import (
    RelayAttachRequest,
    RelayDetachRequest,
    RelayRenewRequest,
)
from mowdeck_contracts.v1.subjects import (
    RELAY_CAMERA_ATTACH,
    RELAY_CAMERA_DETACH,
    RELAY_CAMERA_RENEW,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, MessageHandler, Subscription
from pydantic import BaseModel, ValidationError

from camera_relay.application.service import RelayService

_log = get_logger("camera_relay.bus")

QUEUE = "camera-relay"
"""Queue group of the relay's requests: exactly one replica answers."""


def _handler[RequestT: BaseModel](
    model: type[RequestT], use_case: Callable[[RequestT], Awaitable[BaseModel]]
) -> MessageHandler:
    async def handle(message: BusMessage) -> None:
        try:
            request = message.model(model)
        except ValidationError as exc:
            raise DropMessage(
                f"invalid {model.__name__}: {exc.error_count()} validation errors"
            ) from exc
        reply = await use_case(request)
        await message.respond(reply)

    return handle


def attach_handler(service: RelayService) -> MessageHandler:
    """Answer ``relay.camera.attach``."""
    return _handler(RelayAttachRequest, service.attach)


def renew_handler(service: RelayService) -> MessageHandler:
    """Answer ``relay.camera.renew``."""
    return _handler(RelayRenewRequest, service.renew)


def detach_handler(service: RelayService) -> MessageHandler:
    """Answer ``relay.camera.detach``."""
    return _handler(RelayDetachRequest, service.detach)


async def subscribe_all(bus: EventBus, service: RelayService) -> list[Subscription]:
    """Subscribe the three request handlers and return the subscriptions."""
    return [
        await bus.subscribe(RELAY_CAMERA_ATTACH, attach_handler(service), queue=QUEUE),
        await bus.subscribe(RELAY_CAMERA_RENEW, renew_handler(service), queue=QUEUE),
        await bus.subscribe(RELAY_CAMERA_DETACH, detach_handler(service), queue=QUEUE),
    ]
