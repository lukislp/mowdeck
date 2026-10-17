"""NATS subject names and JetStream stream definitions.

Subject scheme:

====================================  ============================================
``command.mower.<mower_id>``          commands for one mower (stream ``COMMANDS``)
``command.result.mower.<mower_id>``   command results (stream ``COMMANDS``)
``telemetry.mower.<mower_id>``        state snapshots (stream ``TELEMETRY``)
``event.<domain>.<name>``             domain events, e.g. ``event.mower.registered``
``device.mower.<mower_id>.map``       map snapshot of one mower (stream ``DEVICE``)
``device.mower.<mower_id>.capabilities``  capability snapshot (stream ``DEVICE``)
``device.mower.<mower_id>.path``      mow path of the running job (stream ``DEVICE``)
``fleet.mowers.list``                 core NATS request: mower registrations of a shard
``fleet.mowers.credentials``          core NATS request: the withheld secrets of one mower
``fleet.commands.issue``              core NATS request: a command on behalf of a subject
``fleet.targets.resolve``             core NATS request: the mowers of a schedule target
``fleet.drive.start``                 core NATS request: open a manual drive session
``fleet.drive.stop``                  core NATS request: close a manual drive session
``entitlements.check``                core NATS request: may an account use a feature or limit
``entitlements.plan.set``             core NATS request: Billing sets the plan of an account
``tracks.positions.query``            core NATS request: the position track of a mower (Telemetry)
``gateway.mower.<id>.camera.session``  core NATS request: a live camera session (Device-Gateway)
``gateway.mower.<id>.path.preview``   core NATS request: plan a route without starting (Gateway)
``gateway.mower.<id>.bridge.scan``    core NATS request: look for the mower through a Bluetooth
                                      bridge (Device-Gateway)
``gateway.mower.<id>.firmware.check`` core NATS request: ask the vendor for firmware updates
                                      (Device-Gateway)
``gateway.mower.<mower_id>.photo``    core NATS request: a still image of one mower (Vision)
``gateway.cloud.discover``            core NATS request: devices of a vendor cloud account
``relay.camera.attach``               core NATS request: a lease on the camera relay (ADR 0045)
``relay.camera.renew``                core NATS request: a lease's heartbeat, a fresh ticket
``relay.camera.detach``               core NATS request: end a lease
``camera.mower.<id>.relay.<kind>``    camera relay events (stream ``CAMERA``)
====================================  ============================================

The camera request lives below ``gateway.`` and not below ``device.``: the stream ``DEVICE``
captures ``device.>``, and a request published on a stream subject is also stored and
acknowledged by JetStream, whose acknowledgement would race the gateway's reply.

All helpers are pure functions without I/O. The stream definitions are suggestions expressed as
data; the service kit turns them into JetStream stream configurations.
"""

import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")

COMMAND_PREFIX = "command.mower"
COMMAND_RESULT_PREFIX = "command.result.mower"
TELEMETRY_PREFIX = "telemetry.mower"
EVENT_PREFIX = "event"
DEVICE_PREFIX = "device.mower"
DEVICE_SNAPSHOT_KINDS = ("map", "capabilities", "path")
"""The snapshot kinds below ``device.mower.<id>``."""

FLEET_MOWERS_LIST = "fleet.mowers.list"
"""Core NATS request subject (no stream): ``MowerListRequest`` in, ``MowerListReply`` out."""
FLEET_MOWERS_CREDENTIALS = "fleet.mowers.credentials"
"""Core NATS request subject (internal, Device-Gateway to Fleet, queue ``fleet``):
``MowerCredentialsRequest`` in, ``MowerCredentialsReply`` out (ADR 0034)."""
FLEET_COMMANDS_ISSUE = "fleet.commands.issue"
"""Core NATS request subject (internal): ``IssueCommandRequest`` in, ``IssueCommandReply`` out."""
FLEET_TARGETS_RESOLVE = "fleet.targets.resolve"
"""Core NATS request subject (internal): ``ResolveTargetRequest`` in, ``ResolveTargetReply``
out."""
FLEET_DRIVE_START = "fleet.drive.start"
"""Core NATS request subject (internal): ``DriveStartRequest`` in, ``DriveStartReply`` out."""
FLEET_DRIVE_STOP = "fleet.drive.stop"
"""Core NATS request subject (internal): ``DriveStopRequest`` in, ``DriveStopReply`` out."""
GATEWAY_PREFIX = "gateway.mower"
"""Prefix of the core NATS requests the Device-Gateway answers per mower (no stream)."""
ENTITLEMENTS_CHECK = "entitlements.check"
"""Core NATS request subject (internal): ``EntitlementCheckRequest`` in,
``EntitlementCheckReply`` out."""
ENTITLEMENTS_PLAN_SET = "entitlements.plan.set"
"""Core NATS request subject (internal, Billing only): ``PlanSetRequest`` in, ``PlanSetReply``
out."""
TELEMETRY_POSITIONS_QUERY = "tracks.positions.query"
"""Core NATS request subject (internal, answered by Telemetry): ``PositionTrackRequest`` in,
``PositionTrackReply`` out. Deliberately not below ``telemetry.``: the ``TELEMETRY`` stream
captures ``telemetry.>``, and a request on a stream subject is answered by JetStream's publish
acknowledgement instead of the responder."""

GATEWAY_PREFIX = "gateway.mower"
"""Prefix of the core NATS requests a Device-Gateway answers for the mowers it holds. Not
below ``device.``: that prefix belongs to the ``DEVICE`` stream, and request/reply must not be
captured by a stream."""
ALL_PHOTO_REQUESTS = f"{GATEWAY_PREFIX}.*.photo"
"""Wildcard the Device-Gateway subscribes to: photo requests for any mower."""
GATEWAY_CLOUD_DISCOVER = "gateway.cloud.discover"
"""Core NATS request subject (internal, Fleet to Device-Gateway, queue group
``device-gateway``): ``CloudDiscoverRequest`` in, ``CloudDiscoverReply`` out. Logs in to a
vendor cloud account (or reuses the open session) and lists its devices (ADR 0034).
Not below ``device.`` for the same reason as the camera request."""


RELAY_CAMERA_ATTACH = "relay.camera.attach"
"""Core NATS request subject (internal, Fleet or Vision to camera-relay, queue group
``camera-relay``): ``RelayAttachRequest`` in, ``RelayAttachReply`` out (ADR 0045). Not below
``camera.``: the ``CAMERA`` stream captures ``camera.>``, and a request on a stream subject is
answered by JetStream's publish acknowledgement instead of the responder."""
RELAY_CAMERA_RENEW = "relay.camera.renew"
"""Core NATS request subject: ``RelayRenewRequest`` in, ``RelayRenewReply`` out (ADR 0045)."""
RELAY_CAMERA_DETACH = "relay.camera.detach"
"""Core NATS request subject: ``RelayDetachRequest`` in, ``RelayDetachReply`` out (ADR 0045)."""
RELAY_CAMERA_REQUESTS = (RELAY_CAMERA_ATTACH, RELAY_CAMERA_RENEW, RELAY_CAMERA_DETACH)
"""The relay's request subjects; no stream captures them."""
CAMERA_PREFIX = "camera.mower"
CAMERA_RELAY_EVENT_KINDS = ("started", "stopped", "lease_opened", "lease_closed")
"""The event kinds below ``camera.mower.<id>.relay``."""

CameraRelayEventKind = Literal["started", "stopped", "lease_opened", "lease_closed"]
"""An event kind below ``camera.mower.<id>.relay``."""


def camera_relay_event_subject(mower_id: UUID, kind: CameraRelayEventKind) -> str:
    """Subject of a camera relay event: ``camera.mower.<id>.relay.<kind>`` (stream ``CAMERA``)."""
    if kind not in CAMERA_RELAY_EVENT_KINDS:
        msg = f"invalid camera relay event kind {kind!r}"
        raise ValueError(msg)
    return f"{CAMERA_PREFIX}.{mower_id}.relay.{kind}"


def parse_camera_relay_event_subject(subject: str) -> tuple[UUID, str]:
    """Split ``camera.mower.<id>.relay.<kind>`` into the mower id and the event kind."""
    parts = subject.split(".")
    if len(parts) != 5 or ".".join(parts[:2]) != CAMERA_PREFIX or parts[3] != "relay":
        msg = f"not a camera relay event subject: {subject!r}"
        raise ValueError(msg)
    if parts[4] not in CAMERA_RELAY_EVENT_KINDS:
        msg = f"unknown camera relay event kind in {subject!r}"
        raise ValueError(msg)
    return UUID(parts[2]), parts[4]


def photo_subject(mower_id: UUID) -> str:
    """Core NATS request subject for a still image of one mower: ``gateway.mower.<id>.photo``."""
    return f"{GATEWAY_PREFIX}.{mower_id}.photo"


def parse_photo_subject(subject: str) -> UUID:
    """The mower id of ``gateway.mower.<id>.photo``."""
    parts = subject.split(".")
    if len(parts) != 4 or ".".join(parts[:2]) != GATEWAY_PREFIX or parts[3] != "photo":
        msg = f"not a photo request subject: {subject!r}"
        raise ValueError(msg)
    return UUID(parts[2])


def _check_tokens(value: str, what: str) -> str:
    parts = value.split(".")
    if not value or not all(_TOKEN.match(part) for part in parts):
        msg = f"invalid {what} {value!r}: expected lower-case tokens [a-z][a-z0-9_]* joined by '.'"
        raise ValueError(msg)
    return value


def command_subject(mower_id: UUID) -> str:
    """Subject for commands to one mower: ``command.mower.<id>``."""
    return f"{COMMAND_PREFIX}.{mower_id}"


def command_result_subject(mower_id: UUID) -> str:
    """Subject for command results of one mower: ``command.result.mower.<id>``."""
    return f"{COMMAND_RESULT_PREFIX}.{mower_id}"


def telemetry_subject(mower_id: UUID) -> str:
    """Subject for telemetry of one mower: ``telemetry.mower.<id>``."""
    return f"{TELEMETRY_PREFIX}.{mower_id}"


DeviceSnapshotKind = Literal["map", "capabilities", "path"]
"""A snapshot kind below ``device.mower.<id>``."""


def device_subject(mower_id: UUID, kind: DeviceSnapshotKind) -> str:
    """Subject of a device snapshot: ``device.mower.<id>.map``, ``.capabilities`` or
    ``.path``."""
    if kind not in DEVICE_SNAPSHOT_KINDS:
        msg = f"invalid device snapshot kind {kind!r}"
        raise ValueError(msg)
    return f"{DEVICE_PREFIX}.{mower_id}.{kind}"


def parse_device_subject(subject: str) -> tuple[UUID, str]:
    """Split ``device.mower.<id>.<kind>`` into the mower id and the snapshot kind."""
    parts = subject.split(".")
    if len(parts) != 4 or ".".join(parts[:2]) != DEVICE_PREFIX:
        msg = f"not a device snapshot subject: {subject!r}"
        raise ValueError(msg)
    if parts[3] not in DEVICE_SNAPSHOT_KINDS:
        msg = f"unknown device snapshot kind in {subject!r}"
        raise ValueError(msg)
    return UUID(parts[2]), parts[3]


def camera_session_subject(mower_id: UUID) -> str:
    """Core NATS request subject of a camera session: ``gateway.mower.<id>.camera.session``.

    ``CameraSessionRequest`` in, ``CameraSessionReply`` out; answered by the gateway instance
    that holds the mower (other shards stay silent).
    """
    return f"{GATEWAY_PREFIX}.{mower_id}.camera.session"


def parse_camera_session_subject(subject: str) -> UUID:
    """The mower id of a ``gateway.mower.<id>.camera.session`` subject."""
    parts = subject.split(".")
    if (
        len(parts) != 5
        or ".".join(parts[:2]) != GATEWAY_PREFIX
        or parts[3:]
        != [
            "camera",
            "session",
        ]
    ):
        msg = f"not a camera session subject: {subject!r}"
        raise ValueError(msg)
    return UUID(parts[2])


def path_preview_subject(mower_id: UUID) -> str:
    """Core NATS request subject of a path preview: ``gateway.mower.<id>.path.preview``.

    ``PathPreviewGatewayRequest`` in, ``PathPreviewReply`` out; answered by the gateway instance
    that holds the mower (other shards stay silent).
    """
    return f"{GATEWAY_PREFIX}.{mower_id}.path.preview"


def parse_path_preview_subject(subject: str) -> UUID:
    """The mower id of a ``gateway.mower.<id>.path.preview`` subject."""
    parts = subject.split(".")
    if len(parts) != 5 or ".".join(parts[:2]) != GATEWAY_PREFIX or parts[3:] != ["path", "preview"]:
        msg = f"not a path preview subject: {subject!r}"
        raise ValueError(msg)
    return UUID(parts[2])


def bridge_scan_subject(mower_id: UUID) -> str:
    """Core NATS request subject of a bridge scan: ``gateway.mower.<id>.bridge.scan``.

    ``BridgeScanRequest`` in, ``BridgeScanReply`` out; answered by the gateway instance that
    holds the mower (other shards stay silent).
    """
    return f"{GATEWAY_PREFIX}.{mower_id}.bridge.scan"


def parse_bridge_scan_subject(subject: str) -> UUID:
    """The mower id of a ``gateway.mower.<id>.bridge.scan`` subject."""
    parts = subject.split(".")
    if len(parts) != 5 or ".".join(parts[:2]) != GATEWAY_PREFIX or parts[3:] != ["bridge", "scan"]:
        msg = f"not a bridge scan subject: {subject!r}"
        raise ValueError(msg)
    return UUID(parts[2])


def firmware_check_subject(mower_id: UUID) -> str:
    """Core NATS request subject of a firmware check: ``gateway.mower.<id>.firmware.check``.

    ``FirmwareCheckRequest`` in, ``FirmwareCheckReply`` out; answered by the gateway instance
    that holds the mower (other shards stay silent).
    """
    return f"{GATEWAY_PREFIX}.{mower_id}.firmware.check"


def parse_firmware_check_subject(subject: str) -> UUID:
    """The mower id of a ``gateway.mower.<id>.firmware.check`` subject."""
    parts = subject.split(".")
    if (
        len(parts) != 5
        or ".".join(parts[:2]) != GATEWAY_PREFIX
        or parts[3:] != ["firmware", "check"]
    ):
        msg = f"not a firmware check subject: {subject!r}"
        raise ValueError(msg)
    return UUID(parts[2])


def event_subject(domain: str, name: str) -> str:
    """Subject for a domain event: ``event.<domain>.<name>``.

    ``domain`` is a single token; ``name`` may consist of several dot separated tokens.
    """
    if "." in domain:
        msg = f"invalid domain {domain!r}: must be a single token"
        raise ValueError(msg)
    _check_tokens(domain, "domain")
    _check_tokens(name, "event name")
    return f"{EVENT_PREFIX}.{domain}.{name}"


def event_subject_for_type(event_type: str) -> str:
    """Subject for an event type such as ``mower.registered`` (``event.mower.registered``)."""
    domain, sep, name = event_type.partition(".")
    if not sep:
        msg = f"invalid event type {event_type!r}: expected '<domain>.<name>'"
        raise ValueError(msg)
    return event_subject(domain, name)


ALL_COMMANDS = f"{COMMAND_PREFIX}.*"
"""Wildcard for commands to any mower."""
ALL_COMMAND_RESULTS = f"{COMMAND_RESULT_PREFIX}.*"
"""Wildcard for command results of any mower."""
ALL_TELEMETRY = f"{TELEMETRY_PREFIX}.*"
"""Wildcard for telemetry of any mower."""
ALL_DEVICE_SNAPSHOTS = f"{DEVICE_PREFIX}.>"
"""Wildcard for every device snapshot of any mower."""
ALL_CAMERA_SESSIONS = f"{GATEWAY_PREFIX}.*.camera.session"
"""Wildcard the Device-Gateway subscribes to for camera session requests (core NATS)."""
ALL_PATH_PREVIEWS = f"{GATEWAY_PREFIX}.*.path.preview"
"""Wildcard the Device-Gateway subscribes to for path preview requests (core NATS)."""
ALL_BRIDGE_SCANS = f"{GATEWAY_PREFIX}.*.bridge.scan"
"""Wildcard the Device-Gateway subscribes to for bridge scan requests (core NATS)."""
ALL_FIRMWARE_CHECKS = f"{GATEWAY_PREFIX}.*.firmware.check"
"""Wildcard the Device-Gateway subscribes to for firmware check requests (core NATS)."""
ALL_CAMERA_RELAY_EVENTS = f"{CAMERA_PREFIX}.*.relay.*"
"""Wildcard for every camera relay event of any mower (stream ``CAMERA``)."""


def event_wildcard(domain: str | None = None) -> str:
    """Wildcard for all events (``event.>``) or all events of one domain (``event.<d>.>``)."""
    if domain is None:
        return f"{EVENT_PREFIX}.>"
    if "." in domain:
        msg = f"invalid domain {domain!r}: must be a single token"
        raise ValueError(msg)
    _check_tokens(domain, "domain")
    return f"{EVENT_PREFIX}.{domain}.>"


def mower_id_from_subject(subject: str) -> UUID:
    """Extract the mower id from a command, command result or telemetry subject."""
    for prefix in (COMMAND_PREFIX, COMMAND_RESULT_PREFIX, TELEMETRY_PREFIX):
        head, _, tail = subject.rpartition(".")
        if head == prefix:
            return UUID(tail)
    msg = f"not a per-mower subject: {subject!r}"
    raise ValueError(msg)


def subject_matches(pattern: str, subject: str) -> bool:
    """Return True if ``subject`` matches the NATS ``pattern`` (``*`` and ``>`` wildcards)."""
    pattern_tokens = pattern.split(".")
    subject_tokens = subject.split(".")
    for index, part in enumerate(pattern_tokens):
        if part == ">":
            return len(subject_tokens) > index
        if index >= len(subject_tokens):
            return False
        if part not in ("*", subject_tokens[index]):
            return False
    return len(pattern_tokens) == len(subject_tokens)


# --- Streams ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StreamSpec:
    """Suggested JetStream stream settings, as plain data.

    Attributes:
        name: Stream name.
        subjects: Subjects captured by the stream.
        description: Human readable purpose.
        retention: JetStream retention policy.
        storage: ``file`` (survives restarts) or ``memory``.
        max_age_seconds: Messages older than this are removed.
        max_msgs_per_subject: Per subject message limit, ``-1`` for unlimited.
        max_bytes: Total size limit in bytes, ``-1`` for unlimited.
        duplicate_window_seconds: Window in which a repeated ``Nats-Msg-Id`` is dropped.
        discard: Which messages to drop when a limit is hit.
    """

    name: str
    subjects: tuple[str, ...]
    description: str
    retention: Literal["limits", "interest", "workqueue"] = "limits"
    storage: Literal["file", "memory"] = "file"
    max_age_seconds: int = 24 * 3600
    max_msgs_per_subject: int = -1
    max_bytes: int = -1
    duplicate_window_seconds: int = 120
    discard: Literal["old", "new"] = "old"

    def captures(self, subject: str) -> bool:
        """Return True if ``subject`` is stored by this stream."""
        return any(subject_matches(pattern, subject) for pattern in self.subjects)


COMMANDS = StreamSpec(
    name="COMMANDS",
    subjects=("command.>",),
    description="Mower commands and command results. Kept one day for audit and replay; "
    "expired commands are dropped by consumers.",
    max_age_seconds=24 * 3600,
    max_bytes=256 * 1024 * 1024,
    duplicate_window_seconds=120,
)

TELEMETRY = StreamSpec(
    name="TELEMETRY",
    subjects=("telemetry.>",),
    description="Mower state snapshots. Short retention; history lives in the Telemetry service.",
    max_age_seconds=3600,
    max_msgs_per_subject=1000,
    max_bytes=512 * 1024 * 1024,
    duplicate_window_seconds=30,
)

EVENTS = StreamSpec(
    name="EVENTS",
    subjects=("event.>",),
    description="Domain events. Kept 30 days so new consumers can catch up.",
    max_age_seconds=30 * 24 * 3600,
    max_bytes=1024 * 1024 * 1024,
    duplicate_window_seconds=120,
)

DEVICE = StreamSpec(
    name="DEVICE",
    subjects=("device.>",),
    description="Last-value snapshots of every mower (map, capabilities): one message per "
    "subject, kept 30 days so a consumer that starts late still finds the current one.",
    max_age_seconds=30 * 24 * 3600,
    max_msgs_per_subject=1,
    max_bytes=256 * 1024 * 1024,
    duplicate_window_seconds=120,
)

CAMERA = StreamSpec(
    name="CAMERA",
    subjects=("camera.>",),
    description="Camera relay lifecycle (sessions started and stopped, leases opened and "
    "closed) for audit, metrics and the Vision daily limit. Kept 30 days.",
    max_age_seconds=30 * 24 * 3600,
    max_bytes=64 * 1024 * 1024,
    duplicate_window_seconds=120,
)

STREAMS: tuple[StreamSpec, ...] = (COMMANDS, TELEMETRY, EVENTS, DEVICE, CAMERA)
"""All streams MowDeck services expect."""


def stream_for_subject(subject: str) -> StreamSpec | None:
    """Return the stream that stores ``subject`` (a concrete subject or a filter pattern)."""
    for stream in STREAMS:
        if stream.captures(subject):
            return stream
    return None
