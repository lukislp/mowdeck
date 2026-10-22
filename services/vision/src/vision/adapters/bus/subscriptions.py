"""Subscriptions of vision.

Every instance (ephemeral consumers, no queue group) feeds its :class:`BusMowerView`:

* ``telemetry.mower.*`` and ``device.mower.>`` starting with the newest message per subject;
* ``command.mower.*`` and ``command.result.mower.*`` from now on.

Shared (durable ``vision-identity``): ``identity.user.disabled`` and ``identity.user.deleted``
delete the user's provider settings and opt-ins.

Shared (durable ``vision-auto``, queue group: one replica per state): ``telemetry.mower.*`` for
the automatic analysis on errors (ADR 0035); only new states, a restart resumes where the
consumer stopped (states older than ``VISION_AUTO_MAX_STATE_AGE_SECONDS`` are ignored).

Handler rules: return to acknowledge, raise ``DropMessage`` to discard a poison message.
"""

from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import (
    IDENTITY_USER_DELETED,
    IDENTITY_USER_DISABLED,
    CommandEnvelope,
    CommandResult,
    EventEnvelope,
    MowerCapabilitiesSnapshot,
    MowerMapSnapshot,
    MowerPathSnapshot,
    MowerState,
    UserChanged,
)
from mowdeck_contracts.v1.subjects import (
    ALL_COMMAND_RESULTS,
    ALL_COMMANDS,
    ALL_DEVICE_SNAPSHOTS,
    ALL_TELEMETRY,
    event_wildcard,
    mower_id_from_subject,
    parse_device_subject,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, MessageHandler, Subscription
from pydantic import ValidationError

from vision.adapters.bus.view import BusMowerView
from vision.application.auto import AutoAnalysis
from vision.application.ports import Store

_log = get_logger("vision.subscriptions")

IDENTITY_CONSUMER = "vision-identity"
AUTO_CONSUMER = "vision-auto"
_USER_REMOVED = frozenset({IDENTITY_USER_DISABLED, IDENTITY_USER_DELETED})


def _per_mower(message: BusMessage) -> UUID:
    try:
        return mower_id_from_subject(message.subject)
    except ValueError as exc:
        raise DropMessage(f"unexpected subject {message.subject!r}") from exc


def state_handler(view: BusMowerView) -> MessageHandler:
    """Remember the newest state."""

    async def handle(message: BusMessage) -> None:
        try:
            state = message.model(MowerState)
        except ValidationError as exc:
            raise DropMessage(f"invalid mower state: {exc.error_count()} errors") from exc
        if _per_mower(message) != state.mower_id:
            raise DropMessage("subject does not match the state's mower")
        view.on_state(state)

    return handle


def snapshot_handler(view: BusMowerView) -> MessageHandler:
    """Remember the newest map, capabilities and route of the running job."""

    async def handle(message: BusMessage) -> None:
        try:
            mower_id, kind = parse_device_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if kind == "capabilities":
            try:
                capabilities = message.model(MowerCapabilitiesSnapshot)
            except ValidationError as exc:
                msg = f"invalid capabilities snapshot: {exc.error_count()} errors"
                raise DropMessage(msg) from exc
            if capabilities.mower_id != mower_id:
                raise DropMessage("subject does not match the snapshot's mower")
            view.on_capabilities(capabilities)
            return
        if kind == "path":
            try:
                route = message.model(MowerPathSnapshot)
            except ValidationError as exc:
                msg = f"invalid path snapshot: {exc.error_count()} errors"
                raise DropMessage(msg) from exc
            if route.mower_id != mower_id:
                raise DropMessage("subject does not match the snapshot's mower")
            view.on_path(route)
            return
        if kind != "map":
            return
        try:
            snapshot = message.model(MowerMapSnapshot)
        except ValidationError as exc:
            raise DropMessage(f"invalid map snapshot: {exc.error_count()} errors") from exc
        if snapshot.mower_id != mower_id:
            raise DropMessage("subject does not match the snapshot's mower")
        view.on_map(snapshot)

    return handle


def command_handler(view: BusMowerView) -> MessageHandler:
    """Recent commands and emergency stops."""

    async def handle(message: BusMessage) -> None:
        try:
            envelope = message.model(CommandEnvelope)
        except ValidationError as exc:
            raise DropMessage(f"invalid command envelope: {exc.error_count()} errors") from exc
        if _per_mower(message) != envelope.mower_id:
            raise DropMessage("subject does not match the command's mower")
        view.on_command(envelope)

    return handle


def result_handler(view: BusMowerView) -> MessageHandler:
    """Command results for running executions."""

    async def handle(message: BusMessage) -> None:
        try:
            result = message.model(CommandResult)
        except ValidationError as exc:
            raise DropMessage(f"invalid command result: {exc.error_count()} errors") from exc
        if _per_mower(message) != result.mower_id:
            raise DropMessage("subject does not match the result's mower")
        view.on_result(result)

    return handle


def auto_handler(auto: AutoAnalysis) -> MessageHandler:
    """Pass states to the automatic analysis (which returns quickly: the work is a task)."""

    async def handle(message: BusMessage) -> None:
        try:
            state = message.model(MowerState)
        except ValidationError as exc:
            raise DropMessage(f"invalid mower state: {exc.error_count()} errors") from exc
        if _per_mower(message) != state.mower_id:
            raise DropMessage("subject does not match the state's mower")
        await auto.on_state(state)

    return handle


def identity_handler(store: Store) -> MessageHandler:
    """Forget the providers of disabled and deleted users."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[dict[str, Any]])
        except ValidationError as exc:
            raise DropMessage("invalid event envelope") from exc
        if event.type not in _USER_REMOVED:
            return
        try:
            change = UserChanged.model_validate(event.payload)
        except ValidationError as exc:
            raise DropMessage(f"invalid {event.type} payload") from exc
        removed = await store.delete_user(change.user_id)
        _log.info("user removed from vision", user_id=str(change.user_id), providers=removed)

    return handle


async def subscribe_all(
    bus: EventBus, view: BusMowerView, store: Store, auto: AutoAnalysis | None = None
) -> list[Subscription]:
    """Subscribe every handler and return the subscriptions (the automatic analysis only when
    ``auto`` is given)."""
    extra: list[Subscription] = []
    if auto is not None:
        extra.append(
            await bus.subscribe(
                ALL_TELEMETRY, auto_handler(auto), durable=AUTO_CONSUMER, queue=AUTO_CONSUMER
            )
        )
    return [
        *extra,
        await bus.subscribe(ALL_TELEMETRY, state_handler(view), last_per_subject=True),
        await bus.subscribe(ALL_DEVICE_SNAPSHOTS, snapshot_handler(view), last_per_subject=True),
        await bus.subscribe(ALL_COMMANDS, command_handler(view)),
        await bus.subscribe(ALL_COMMAND_RESULTS, result_handler(view)),
        await bus.subscribe(
            event_wildcard("identity"),
            identity_handler(store),
            durable=IDENTITY_CONSUMER,
            queue=IDENTITY_CONSUMER,
        ),
    ]
