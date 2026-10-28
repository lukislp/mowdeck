"""Subscriptions of notifications.

* ``telemetry.mower.*`` (JetStream ``TELEMETRY``, durable and queue group
  ``notifications-states``): the rules.
* ``event.mower.>`` (JetStream ``EVENTS``, durable ``notifications-mowers``):
  ``mower.registered``/``updated``/``removed`` maintain the directory (account and name),
  ``mower.connected``/``disconnected`` feed the offline rule.
* ``event.identity.>`` (JetStream ``EVENTS``, durable ``notifications-identity``):
  ``identity.user.disabled`` and ``identity.user.deleted`` delete the user's preferences,
  targets, inbox and deliveries; other identity events are acknowledged and ignored.
* ``command.mower.*`` (JetStream ``COMMANDS``, durable and queue group
  ``notifications-commands``): the time of the newest command per mower (any kind, manual drive
  frames separately), which explains a departure from the dock and suspends the geofence while
  someone drives the mower by hand.
* ``device.mower.>`` (JetStream ``DEVICE``, durable and queue group ``notifications-device``,
  starting with the newest stored message per subject): map snapshots become the geofence;
  capability snapshots are acknowledged and ignored.
* ``event.telemetry.>`` (JetStream ``EVENTS``, durable ``notifications-telemetry``):
  ``telemetry.blade_hours.threshold`` becomes ``blade_maintenance``; other telemetry events are
  acknowledged and ignored.
* ``event.tracking.>`` (JetStream ``EVENTS``, durable ``notifications-tracking``): the theft
  tracking milestones of Telemetry (ADR 0058) become ``tracking_started``/``_moved``/``_stale``/
  ``_ended``.
* ``event.vision.>`` (JetStream ``EVENTS``, durable ``notifications-vision``):
  ``vision.proposal.created`` becomes ``vision_proposal`` for the users who may drive the mower;
  ``vision.proposal.updated`` of an automatic analysis without a camera photo becomes
  ``vision_auto_proposal`` ("no photo"); other vision events are acknowledged and ignored.
* ``event.identity.grants.changed`` (every instance): drops cached authorization decisions.

A state, command, map snapshot or telemetry event naming another account than the mower
directory is dropped.

Handler rules: return to acknowledge, raise to get a redelivery, raise ``DropMessage`` to
discard a poison message. Malformed payloads are dropped with a warning, never redelivered.
"""

from typing import Any

from mowdeck_authz.events import subscribe_cache_invalidation
from mowdeck_authz.writer import SupportsInvalidate
from mowdeck_contracts.v1 import (
    IDENTITY_USER_DELETED,
    IDENTITY_USER_DISABLED,
    MOWER_CONNECTED,
    MOWER_DISCONNECTED,
    MOWER_REGISTERED,
    MOWER_REMOVED,
    MOWER_UPDATED,
    TELEMETRY_BLADE_HOURS_THRESHOLD,
    TRACKING_ENDED,
    TRACKING_MOVED,
    TRACKING_STALE,
    TRACKING_STARTED,
    VISION_PROPOSAL_CREATED,
    VISION_PROPOSAL_UPDATED,
    BladeHoursThreshold,
    CommandEnvelope,
    EventEnvelope,
    MowerConnectionEvent,
    MowerMapSnapshot,
    MowerRegistration,
    MowerState,
    TrackingEvent,
    UserChanged,
    VisionProposalCreated,
    VisionProposalUpdated,
)
from mowdeck_contracts.v1.subjects import (
    ALL_COMMANDS,
    ALL_DEVICE_SNAPSHOTS,
    ALL_TELEMETRY,
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

from notifications.application.services import Accounts
from notifications.application.watcher import Watcher
from notifications.domain.errors import AccountMismatchError

_log = get_logger("notifications.bus")

STATES_CONSUMER = "notifications-states"
"""Durable consumer (and queue group) of the state stream, shared by all replicas."""
MOWERS_CONSUMER = "notifications-mowers"
"""Durable consumer (and queue group) of the mower events."""
IDENTITY_CONSUMER = "notifications-identity"
"""Durable consumer (and queue group) of Identity's events."""
COMMANDS_CONSUMER = "notifications-commands"
"""Durable consumer (and queue group) of the mower commands."""
DEVICE_CONSUMER = "notifications-device"
"""Durable consumer (and queue group) of the device snapshots (maps)."""
TELEMETRY_CONSUMER = "notifications-telemetry"
"""Durable consumer (and queue group) of Telemetry's events."""
TRACKING_CONSUMER = "notifications-tracking"
"""Durable consumer (and queue group) of Telemetry's theft tracking events."""
VISION_CONSUMER = "notifications-vision"
"""Durable consumer (and queue group) of the Vision service's events."""

_DIRECTORY_EVENTS = frozenset({MOWER_REGISTERED, MOWER_UPDATED, MOWER_REMOVED})
_CONNECTION_EVENTS = frozenset({MOWER_CONNECTED, MOWER_DISCONNECTED})
_TRACKING_EVENTS = frozenset({TRACKING_STARTED, TRACKING_MOVED, TRACKING_STALE, TRACKING_ENDED})
_USER_REMOVED = frozenset({IDENTITY_USER_DISABLED, IDENTITY_USER_DELETED})


def state_handler(watcher: Watcher) -> MessageHandler:
    """Apply the rules to a state."""

    async def handle(message: BusMessage) -> None:
        try:
            state = message.model(MowerState)
        except ValidationError as exc:
            raise DropMessage(f"invalid mower state: {exc.error_count()} errors") from exc
        try:
            subject_mower = mower_id_from_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if subject_mower != state.mower_id:
            raise DropMessage("subject does not match the state's mower")
        try:
            await watcher.on_state(state)
        except AccountMismatchError as exc:
            _log.warning("state dropped", mower_id=str(state.mower_id), reason=exc.message)
            raise DropMessage(exc.message) from exc

    return handle


def mower_event_handler(watcher: Watcher) -> MessageHandler:
    """Directory and connection events of mowers."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[dict[str, Any]])
        except ValidationError as exc:
            raise DropMessage("invalid event envelope") from exc
        if event.type in _DIRECTORY_EVENTS:
            try:
                registration = MowerRegistration.model_validate(event.payload)
            except ValidationError as exc:
                raise DropMessage(f"invalid {event.type} payload") from exc
            if event.type == MOWER_REMOVED:
                await watcher.mower_removed(registration.mower_id)
            else:
                await watcher.mower_registered(registration)
            return
        if event.type in _CONNECTION_EVENTS:
            try:
                link = MowerConnectionEvent.model_validate(event.payload)
            except ValidationError as exc:
                raise DropMessage(f"invalid {event.type} payload") from exc
            try:
                await watcher.on_connection(
                    link.mower_id,
                    link.account_id,
                    connected=event.type == MOWER_CONNECTED,
                    at=link.at,
                )
            except AccountMismatchError as exc:
                raise DropMessage(exc.message) from exc

    return handle


def command_handler(watcher: Watcher) -> MessageHandler:
    """Remember when a command was issued for a mower."""

    async def handle(message: BusMessage) -> None:
        try:
            envelope = message.model(CommandEnvelope)
        except ValidationError as exc:
            raise DropMessage(f"invalid command envelope: {exc.error_count()} errors") from exc
        try:
            subject_mower = mower_id_from_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if subject_mower != envelope.mower_id:
            raise DropMessage("subject does not match the command's mower")
        try:
            await watcher.on_command(envelope)
        except AccountMismatchError as exc:
            _log.warning("command dropped", mower_id=str(envelope.mower_id), reason=exc.message)
            raise DropMessage(exc.message) from exc

    return handle


def snapshot_handler(watcher: Watcher) -> MessageHandler:
    """Turn map snapshots into geofences."""

    async def handle(message: BusMessage) -> None:
        try:
            mower_id, kind = parse_device_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if kind != "map":
            return
        try:
            snapshot = message.model(MowerMapSnapshot)
        except ValidationError as exc:
            raise DropMessage(f"invalid map snapshot: {exc.error_count()} errors") from exc
        if snapshot.mower_id != mower_id:
            raise DropMessage("subject does not match the snapshot's mower")
        try:
            await watcher.on_map(snapshot)
        except AccountMismatchError as exc:
            _log.warning("map dropped", mower_id=str(mower_id), reason=exc.message)
            raise DropMessage(exc.message) from exc

    return handle


def telemetry_event_handler(watcher: Watcher) -> MessageHandler:
    """Blade maintenance reminders from Telemetry."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[dict[str, Any]])
        except ValidationError as exc:
            raise DropMessage("invalid event envelope") from exc
        if event.type != TELEMETRY_BLADE_HOURS_THRESHOLD:
            return
        try:
            crossing = BladeHoursThreshold.model_validate(event.payload)
        except ValidationError as exc:
            raise DropMessage(f"invalid {event.type} payload") from exc
        try:
            await watcher.on_blade_threshold(crossing)
        except AccountMismatchError as exc:
            raise DropMessage(exc.message) from exc

    return handle


def tracking_event_handler(watcher: Watcher) -> MessageHandler:
    """Theft tracking milestones from Telemetry (ADR 0058)."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[dict[str, Any]])
        except ValidationError as exc:
            raise DropMessage("invalid event envelope") from exc
        if event.type not in _TRACKING_EVENTS:
            return
        try:
            payload = TrackingEvent.model_validate(event.payload)
        except ValidationError as exc:
            raise DropMessage(f"invalid {event.type} payload") from exc
        try:
            await watcher.on_tracking(event.type, payload)
        except AccountMismatchError as exc:
            raise DropMessage(exc.message) from exc

    return handle


def vision_event_handler(watcher: Watcher) -> MessageHandler:
    """Proposals of AI assist."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[dict[str, Any]])
        except ValidationError as exc:
            raise DropMessage("invalid event envelope") from exc
        if event.type not in (VISION_PROPOSAL_CREATED, VISION_PROPOSAL_UPDATED):
            return
        try:
            proposal = (
                VisionProposalCreated.model_validate(event.payload)
                if event.type == VISION_PROPOSAL_CREATED
                else VisionProposalUpdated.model_validate(event.payload)
            )
        except ValidationError as exc:
            raise DropMessage(f"invalid {event.type} payload") from exc
        if event.account_id is not None and event.account_id != proposal.account_id:
            raise DropMessage("envelope and payload name different accounts")
        try:
            if isinstance(proposal, VisionProposalCreated):
                await watcher.on_vision_proposal(proposal)
            else:
                await watcher.on_vision_update(proposal)
        except AccountMismatchError as exc:
            raise DropMessage(exc.message) from exc

    return handle


def identity_handler(accounts: Accounts) -> MessageHandler:
    """Forget users that were disabled or deleted."""

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
        await accounts.user_removed(change.user_id, change.account_id)
        _log.info(
            "user removed from notifications",
            user_id=str(change.user_id),
            account_id=str(change.account_id) if change.account_id else None,
            event_type=event.type,
        )

    return handle


async def subscribe_all(
    bus: EventBus, watcher: Watcher, accounts: Accounts, cache: SupportsInvalidate | None
) -> list[Subscription]:
    """Subscribe every handler of the service and return the subscriptions."""
    subscriptions: list[Subscription] = [
        await bus.subscribe(
            ALL_TELEMETRY, state_handler(watcher), durable=STATES_CONSUMER, queue=STATES_CONSUMER
        ),
        await bus.subscribe(
            event_wildcard("mower"),
            mower_event_handler(watcher),
            durable=MOWERS_CONSUMER,
            queue=MOWERS_CONSUMER,
        ),
        await bus.subscribe(
            event_wildcard("identity"),
            identity_handler(accounts),
            durable=IDENTITY_CONSUMER,
            queue=IDENTITY_CONSUMER,
        ),
        await bus.subscribe(
            ALL_COMMANDS,
            command_handler(watcher),
            durable=COMMANDS_CONSUMER,
            queue=COMMANDS_CONSUMER,
        ),
        await bus.subscribe(
            ALL_DEVICE_SNAPSHOTS,
            snapshot_handler(watcher),
            durable=DEVICE_CONSUMER,
            queue=DEVICE_CONSUMER,
            last_per_subject=True,
        ),
        await bus.subscribe(
            event_wildcard("telemetry"),
            telemetry_event_handler(watcher),
            durable=TELEMETRY_CONSUMER,
            queue=TELEMETRY_CONSUMER,
        ),
        await bus.subscribe(
            event_wildcard("tracking"),
            tracking_event_handler(watcher),
            durable=TRACKING_CONSUMER,
            queue=TRACKING_CONSUMER,
        ),
        await bus.subscribe(
            event_wildcard("vision"),
            vision_event_handler(watcher),
            durable=VISION_CONSUMER,
            queue=VISION_CONSUMER,
        ),
    ]
    if cache is not None:
        subscriptions.append(await subscribe_cache_invalidation(bus, cache))
    return subscriptions
