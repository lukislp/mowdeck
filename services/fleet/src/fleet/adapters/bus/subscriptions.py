"""Subscriptions of Fleet.

* ``fleet.mowers.list`` (core NATS request, queue ``fleet``): the registrations of a gateway
  shard (without secrets); exactly one replica answers.
* ``fleet.mowers.credentials`` (core NATS request, queue ``fleet``, internal): the withheld
  secrets of one mower for the Device-Gateway that opens its connection (ADR 0034); the
  values are never logged.
* ``command.result.mower.*`` (JetStream ``COMMANDS``, durable ``fleet-command-results``):
  updates the command records.
* ``event.identity.account.created`` (JetStream ``EVENTS``, durable ``fleet-accounts``):
  creates the default site of the account.
* ``camera.mower.*.relay.*`` (JetStream ``CAMERA``, durable ``fleet-camera-relay``): the camera
  relay's stream starts and stops become audit rows of the event's account
  (``mower.camera_stream_started``, ``mower.camera_stream_stopped``), once per event id; the
  lease events are acknowledged and ignored (ADR 0045).
* ``event.identity.grants.changed`` (JetStream ``EVENTS``, every instance): drops cached
  authorization decisions.
* ``fleet.commands.issue`` (core NATS request, queue ``fleet``, internal): a command on behalf
  of a subject, sent by another service (the Scheduler); logged and counted per origin and
  outcome (``mowdeck_fleet_issued_commands_total``).
* ``fleet.targets.resolve`` (core NATS request, queue ``fleet``, internal): the mowers of a
  schedule target with the time zone and coordinates of their sites.
* ``fleet.drive.start`` and ``fleet.drive.stop`` (core NATS requests, queue ``fleet``,
  internal): Realtime opens and closes a manual drive session on behalf of a subject
  (ADR 0026); counted per outcome (``mowdeck_fleet_drive_sessions_total``).

Core NATS is internal (clients reach services only through Traefik), so only services can send
these requests; the issue request still performs every authorization check for the subject.

Handler rules: return to acknowledge, raise to get a redelivery, raise ``DropMessage`` to
discard a poison message.
"""

from typing import Any

from mowdeck_authz.events import subscribe_cache_invalidation
from mowdeck_authz.writer import SupportsInvalidate
from mowdeck_contracts.v1 import (
    IDENTITY_ACCOUNT_CREATED,
    AccountCreated,
    CommandResult,
    DriveStartRequest,
    DriveStopRequest,
    EventEnvelope,
    IssueCommandRequest,
    MowerCredentialsRequest,
    MowerListReply,
    MowerListRequest,
    ResolveTargetReply,
    ResolveTargetRequest,
)
from mowdeck_contracts.v1.subjects import (
    ALL_CAMERA_RELAY_EVENTS,
    ALL_COMMAND_RESULTS,
    FLEET_COMMANDS_ISSUE,
    FLEET_DRIVE_START,
    FLEET_DRIVE_STOP,
    FLEET_MOWERS_CREDENTIALS,
    FLEET_MOWERS_LIST,
    FLEET_TARGETS_RESOLVE,
    event_subject_for_type,
    parse_camera_relay_event_subject,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import (
    BusMessage,
    DropMessage,
    EventBus,
    MessageHandler,
    Subscription,
)
from prometheus_client import CollectorRegistry, Counter
from pydantic import ValidationError

from fleet.application.services import Services

_log = get_logger("fleet.bus")

LIST_QUEUE = "fleet"
"""Queue group of ``fleet.mowers.list``: exactly one replica answers."""
RESULTS_CONSUMER = "fleet-command-results"
"""Durable consumer (and deliver group) of the command results, shared by all replicas."""
ACCOUNTS_CONSUMER = "fleet-accounts"
"""Durable consumer (and deliver group) of ``identity.account.created``."""
CAMERA_RELAY_CONSUMER = "fleet-camera-relay"
"""Durable consumer (and deliver group) of the camera relay's events (stream ``CAMERA``)."""


def credentials_handler(services: Services) -> MessageHandler:
    """Answer ``fleet.mowers.credentials`` with the withheld secrets of one mower."""

    async def handle(message: BusMessage) -> None:
        try:
            request = message.model(MowerCredentialsRequest)
        except ValidationError as exc:
            raise DropMessage(f"invalid credentials request: {exc.error_count()} errors") from exc
        await message.respond(await services.linked_accounts.credentials(request))

    return handle


def list_handler(services: Services) -> MessageHandler:
    """Answer ``fleet.mowers.list`` with the registrations of the requested shard."""

    async def handle(message: BusMessage) -> None:
        try:
            request = message.model(MowerListRequest)
        except ValidationError as exc:
            raise DropMessage(f"invalid mower list request: {exc.error_count()} errors") from exc
        mowers = await services.mowers.registrations_for_shard(
            request.shard_index, request.shard_count
        )
        await message.respond(MowerListReply(mowers=mowers))
        _log.info(
            "mower list answered",
            shard_index=request.shard_index,
            shard_count=request.shard_count,
            count=len(mowers),
        )

    return handle


def result_handler(services: Services) -> MessageHandler:
    """Apply a command result to its record."""

    async def handle(message: BusMessage) -> None:
        try:
            result = message.model(CommandResult)
        except ValidationError as exc:
            raise DropMessage(f"invalid command result: {exc.error_count()} errors") from exc
        changed = await services.commands.apply_result(result)
        _log.debug(
            "command result",
            command_id=str(result.command_id),
            status=result.status.value,
            applied=changed,
        )

    return handle


def account_handler(services: Services) -> MessageHandler:
    """Create the default site of a new account (redelivered while OpenFGA is down)."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[AccountCreated])
        except ValidationError as exc:
            raise DropMessage(f"invalid account.created event: {exc.error_count()} errors") from exc
        created = await services.sites.ensure_default(event.payload.account_id)
        _log.info("default site ready", account_id=str(created.account_id), site_id=str(created.id))

    return handle


def camera_relay_handler(services: Services) -> MessageHandler:
    """Audit a camera relay stream start or stop (idempotent: a redelivery writes nothing)."""

    async def handle(message: BusMessage) -> None:
        try:
            _, kind = parse_camera_relay_event_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"not a camera relay event: {message.subject}") from exc
        if kind not in ("started", "stopped"):
            return
        try:
            event = message.model(EventEnvelope[dict[str, Any]])
            written = await services.camera.record_relay_event(event)
        except (ValidationError, ValueError) as exc:
            raise DropMessage(f"invalid camera relay {kind} event: {exc}") from exc
        _log.info(
            "camera relay event audited",
            kind=kind,
            event_id=str(event.id),
            written=written,
        )

    return handle


class IssueMetrics:
    """``mowdeck_fleet_issued_commands_total{origin, outcome}``."""

    def __init__(self, registry: CollectorRegistry | None) -> None:
        self._issued = Counter(
            "mowdeck_fleet_issued_commands",
            "Commands issued by services on behalf of a subject (fleet.commands.issue).",
            ["origin", "outcome"],
            registry=registry,
        )

    def count(self, origin: str, outcome: str) -> None:
        """Count one answered request."""
        self._issued.labels(origin=origin, outcome=outcome).inc()


def issue_handler(services: Services, metrics: IssueMetrics | None = None) -> MessageHandler:
    """Answer ``fleet.commands.issue``: the checks of the HTTP endpoint, for the subject."""

    async def handle(message: BusMessage) -> None:
        try:
            request = message.model(IssueCommandRequest)
        except ValidationError as exc:
            raise DropMessage(f"invalid issue request: {exc.error_count()} errors") from exc
        reply = await services.commands.issue(request)
        await message.respond(reply)
        outcome = "accepted" if reply.error is None else reply.error.code.value
        if metrics is not None:
            metrics.count(request.origin, outcome)
        _log.info(
            "command issued on behalf of a subject",
            origin=request.origin,
            schedule_id=str(request.schedule_id) if request.schedule_id else None,
            mower_id=str(request.mower_id),
            subject=str(request.on_behalf_of.subject),
            kind=request.command.command.kind,
            outcome=outcome,
            command_id=str(reply.accepted.command_id) if reply.accepted else None,
        )

    return handle


class DriveMetrics:
    """``mowdeck_fleet_drive_sessions_total{outcome}``: answered ``fleet.drive.start``."""

    def __init__(self, registry: CollectorRegistry | None) -> None:
        self._started = Counter(
            "mowdeck_fleet_drive_sessions",
            "Manual drive session starts (fleet.drive.start) by outcome.",
            ["outcome"],
            registry=registry,
        )

    def count(self, outcome: str) -> None:
        """Count one answered start."""
        self._started.labels(outcome=outcome).inc()


def drive_start_handler(services: Services, metrics: DriveMetrics | None = None) -> MessageHandler:
    """Answer ``fleet.drive.start``: ``can_drive`` for the subject, one open session per mower."""

    async def handle(message: BusMessage) -> None:
        try:
            request = message.model(DriveStartRequest)
        except ValidationError as exc:
            raise DropMessage(f"invalid drive start request: {exc.error_count()} errors") from exc
        reply = await services.drive.start(request)
        await message.respond(reply)
        outcome = "started" if reply.error is None else reply.error.code.value
        if metrics is not None:
            metrics.count(outcome)
        _log.info(
            "drive session requested",
            mower_id=str(request.mower_id),
            subject=str(request.on_behalf_of.subject),
            outcome=outcome,
            session_id=str(reply.started.session_id) if reply.started else None,
        )

    return handle


def drive_stop_handler(services: Services) -> MessageHandler:
    """Answer ``fleet.drive.stop``: close the session record."""

    async def handle(message: BusMessage) -> None:
        try:
            request = message.model(DriveStopRequest)
        except ValidationError as exc:
            raise DropMessage(f"invalid drive stop request: {exc.error_count()} errors") from exc
        reply = await services.drive.stop(request)
        await message.respond(reply)
        _log.info(
            "drive session stopped",
            session_id=str(request.session_id),
            reason=request.reason.value,
            frames=request.frames,
            found=reply.found,
        )

    return handle


def resolve_handler(services: Services) -> MessageHandler:
    """Answer ``fleet.targets.resolve``."""

    async def handle(message: BusMessage) -> None:
        try:
            request = message.model(ResolveTargetRequest)
        except ValidationError as exc:
            raise DropMessage(f"invalid resolve request: {exc.error_count()} errors") from exc
        reply: ResolveTargetReply = await services.mowers.resolve_target(request)
        await message.respond(reply)

    return handle


async def subscribe_all(
    bus: EventBus,
    services: Services,
    cache: SupportsInvalidate | None,
    registry: CollectorRegistry | None = None,
) -> list[Subscription]:
    """Subscribe every handler of the service and return the subscriptions."""
    subscriptions: list[Subscription] = [
        await bus.subscribe(FLEET_MOWERS_LIST, list_handler(services), queue=LIST_QUEUE),
        await bus.subscribe(
            FLEET_MOWERS_CREDENTIALS, credentials_handler(services), queue=LIST_QUEUE
        ),
        await bus.subscribe(
            FLEET_COMMANDS_ISSUE,
            issue_handler(services, IssueMetrics(registry)),
            queue=LIST_QUEUE,
        ),
        await bus.subscribe(FLEET_TARGETS_RESOLVE, resolve_handler(services), queue=LIST_QUEUE),
        await bus.subscribe(
            FLEET_DRIVE_START,
            drive_start_handler(services, DriveMetrics(registry)),
            queue=LIST_QUEUE,
        ),
        await bus.subscribe(FLEET_DRIVE_STOP, drive_stop_handler(services), queue=LIST_QUEUE),
        await bus.subscribe(
            ALL_COMMAND_RESULTS,
            result_handler(services),
            durable=RESULTS_CONSUMER,
            queue=RESULTS_CONSUMER,
        ),
        await bus.subscribe(
            event_subject_for_type(IDENTITY_ACCOUNT_CREATED),
            account_handler(services),
            durable=ACCOUNTS_CONSUMER,
            queue=ACCOUNTS_CONSUMER,
        ),
        await bus.subscribe(
            ALL_CAMERA_RELAY_EVENTS,
            camera_relay_handler(services),
            durable=CAMERA_RELAY_CONSUMER,
            queue=CAMERA_RELAY_CONSUMER,
        ),
    ]
    if cache is not None:
        subscriptions.append(await subscribe_cache_invalidation(bus, cache))
    return subscriptions
