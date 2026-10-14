"""Subscriptions of telemetry.

* ``telemetry.mower.*`` (JetStream ``TELEMETRY``, durable and queue group ``telemetry-states``):
  latest state, history, position track.
* ``command.result.mower.*`` (JetStream ``COMMANDS``, durable ``telemetry-results``): the
  command timeline; the account comes from the mower directory.
* ``device.mower.>`` (JetStream ``DEVICE``, durable ``telemetry-device``): the newest map and
  capabilities the Device-Gateway read from each mower.
* ``event.mower.*`` (JetStream ``EVENTS``, durable ``telemetry-mowers``): ``mower.registered``,
  ``mower.updated`` and ``mower.removed`` maintain the directory (other mower events are
  acknowledged and ignored).
* ``tracks.positions.query`` (core NATS request, queue group ``telemetry-track``): the
  position track of one mower of one account for a time range, for the Planner's coverage.

Handler rules: return to acknowledge, raise to get a redelivery, raise ``DropMessage`` to
discard a poison message. Malformed payloads are dropped with a warning, never redelivered.
"""

from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import (
    MOWER_REGISTERED,
    MOWER_REMOVED,
    MOWER_UPDATED,
    CommandResult,
    EventEnvelope,
    MowerCapabilitiesSnapshot,
    MowerMapSnapshot,
    MowerPathSnapshot,
    MowerRegistration,
    MowerState,
    PositionTrackReply,
    PositionTrackRequest,
    encode_polyline,
)
from mowdeck_contracts.v1.positions import TRACK_PRECISION
from mowdeck_contracts.v1.subjects import (
    ALL_COMMAND_RESULTS,
    ALL_DEVICE_SNAPSHOTS,
    ALL_TELEMETRY,
    TELEMETRY_POSITIONS_QUERY,
    event_wildcard,
    mower_id_from_subject,
    parse_device_subject,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import (
    BusMessage,
    DropMessage,
    EventBus,
    MessageHandler,
    Subscription,
)
from pydantic import ValidationError

from telemetry.application.ingest import Ingest
from telemetry.application.queries import Queries
from telemetry.domain.errors import AccountMismatchError, InvalidQueryError, UnknownMowerError

_log = get_logger("telemetry.bus")

STATES_CONSUMER = "telemetry-states"
"""Durable consumer (and queue group) of the state stream, shared by all replicas."""
RESULTS_CONSUMER = "telemetry-results"
"""Durable consumer (and queue group) of the command results."""
MOWERS_CONSUMER = "telemetry-mowers"
"""Durable consumer (and queue group) of the mower events."""
DEVICE_CONSUMER = "telemetry-device"
"""Durable consumer (and queue group) of the device snapshots."""
TRACK_QUEUE = "telemetry-track"
"""Queue group of the position track requests (one replica answers)."""

_DIRECTORY_EVENTS = frozenset({MOWER_REGISTERED, MOWER_UPDATED, MOWER_REMOVED})


def _subject_mower(message: BusMessage) -> UUID:
    try:
        return mower_id_from_subject(message.subject)
    except ValueError as exc:
        raise DropMessage(f"unexpected subject {message.subject!r}") from exc


def state_handler(ingest: Ingest) -> MessageHandler:
    """Store a state."""

    async def handle(message: BusMessage) -> None:
        try:
            state = message.model(MowerState)
        except ValidationError as exc:
            raise DropMessage(f"invalid mower state: {exc.error_count()} errors") from exc
        if _subject_mower(message) != state.mower_id:
            raise DropMessage("subject does not match the state's mower")
        try:
            stored = await ingest.record_state(state)
        except AccountMismatchError as exc:
            _log.warning(
                "state dropped",
                mower_id=str(state.mower_id),
                account_id=str(state.account_id),
                reason=exc.message,
            )
            raise DropMessage(exc.message) from exc
        if not stored:
            _log.debug("older state ignored", mower_id=str(state.mower_id))

    return handle


def result_handler(ingest: Ingest, *, max_attempts: int) -> MessageHandler:
    """Append a command result to the timeline.

    A result for a mower that is not in the directory is redelivered (Fleet's event may be a
    moment behind) up to ``max_attempts`` deliveries, then dropped with a warning.
    """

    async def handle(message: BusMessage) -> None:
        try:
            result = message.model(CommandResult)
        except ValidationError as exc:
            raise DropMessage(f"invalid command result: {exc.error_count()} errors") from exc
        if _subject_mower(message) != result.mower_id:
            raise DropMessage("subject does not match the result's mower")
        try:
            await ingest.record_result(result)
        except UnknownMowerError as exc:
            if message.delivery_attempt >= max_attempts:
                _log.warning(
                    "command result dropped, mower unknown",
                    mower_id=str(result.mower_id),
                    command_id=str(result.command_id),
                    attempts=message.delivery_attempt,
                )
                raise DropMessage(exc.message) from exc
            raise

    return handle


def snapshot_handler(ingest: Ingest) -> MessageHandler:
    """Store the newest map or capability snapshot of a mower."""

    async def handle(message: BusMessage) -> None:
        try:
            mower_id, kind = parse_device_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        snapshot: MowerMapSnapshot | MowerCapabilitiesSnapshot | MowerPathSnapshot
        try:
            if kind == "map":
                snapshot = message.model(MowerMapSnapshot)
            elif kind == "path":
                snapshot = message.model(MowerPathSnapshot)
            else:
                snapshot = message.model(MowerCapabilitiesSnapshot)
        except ValidationError as exc:
            raise DropMessage(f"invalid {kind} snapshot: {exc.error_count()} errors") from exc
        if snapshot.mower_id != mower_id:
            raise DropMessage("subject does not match the snapshot's mower")
        try:
            await ingest.record_snapshot(snapshot)
        except AccountMismatchError as exc:
            _log.warning("snapshot dropped", mower_id=str(mower_id), kind=kind, reason=exc.message)
            raise DropMessage(exc.message) from exc

    return handle


def mower_event_handler(ingest: Ingest) -> MessageHandler:
    """Keep the mower directory in step with Fleet."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[dict[str, Any]])
        except ValidationError as exc:
            raise DropMessage("invalid event envelope") from exc
        if event.type not in _DIRECTORY_EVENTS:
            return  # mower.connected and friends
        try:
            registration = MowerRegistration.model_validate(event.payload)
        except ValidationError as exc:
            raise DropMessage(f"invalid {event.type} payload") from exc
        if event.type == MOWER_REMOVED:
            await ingest.mower_removed(registration.mower_id)
        else:
            await ingest.mower_registered(registration)

    return handle


def track_handler(queries: Queries) -> MessageHandler:
    """Answer ``tracks.positions.query`` with the encoded track (or an error)."""

    async def handle(message: BusMessage) -> None:
        try:
            request = message.model(PositionTrackRequest)
        except ValidationError as exc:
            raise DropMessage(f"invalid track request: {exc.error_count()} errors") from exc
        try:
            result = await queries.track(
                request.account_id, request.mower_id, request.start, request.end, request.limit
            )
        except InvalidQueryError as exc:
            await message.respond(PositionTrackReply(mower_id=request.mower_id, error=exc.message))
            return
        rows = result.positions
        await message.respond(
            PositionTrackReply(
                mower_id=request.mower_id,
                count=len(rows),
                track=encode_polyline(((p.lat, p.lon) for p in rows), TRACK_PRECISION),
                first_at=rows[0].observed_at if rows else None,
                last_at=rows[-1].observed_at if rows else None,
                truncated=result.truncated,
            )
        )
        _log.debug("track served", mower_id=str(request.mower_id), points=len(rows))

    return handle


async def subscribe_all(
    bus: EventBus,
    ingest: Ingest,
    *,
    unknown_mower_attempts: int,
    queries: Queries | None = None,
) -> list[Subscription]:
    """Subscribe every handler of the service and return the subscriptions."""
    subscriptions = [
        await bus.subscribe(
            ALL_TELEMETRY, state_handler(ingest), durable=STATES_CONSUMER, queue=STATES_CONSUMER
        ),
        await bus.subscribe(
            ALL_COMMAND_RESULTS,
            result_handler(ingest, max_attempts=unknown_mower_attempts),
            durable=RESULTS_CONSUMER,
            queue=RESULTS_CONSUMER,
        ),
        await bus.subscribe(
            ALL_DEVICE_SNAPSHOTS,
            snapshot_handler(ingest),
            durable=DEVICE_CONSUMER,
            queue=DEVICE_CONSUMER,
        ),
        await bus.subscribe(
            event_wildcard("mower"),
            mower_event_handler(ingest),
            durable=MOWERS_CONSUMER,
            queue=MOWERS_CONSUMER,
        ),
    ]
    if queries is not None:
        subscriptions.append(
            await bus.subscribe(
                TELEMETRY_POSITIONS_QUERY, track_handler(queries), queue=TRACK_QUEUE
            )
        )
    return subscriptions
