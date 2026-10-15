"""Rules of the Device-Gateway as pure functions."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import (
    DRIVE_CALIBRATION_OPTION_KEYS,
    LINK_BLE_BRIDGE,
    BridgeScanErrorCode,
    BridgeScanReply,
    BridgeScanRequest,
    CameraSessionErrorCode,
    CameraSessionRequest,
    CommandEnvelope,
    ConnectionKind,
    DriveCalibration,
    FirmwareCheckErrorCode,
    FirmwareCheckRequest,
    MowerRegistration,
    MowerState,
    MowPath,
    PathPreviewErrorCode,
    PathPreviewGatewayRequest,
    calibration_of_links,
)


@dataclass(frozen=True, slots=True)
class Shard:
    """The part of all mowers one instance is responsible for.

    A mower belongs to shard ``index`` of ``count`` when ``mower_id.int % count == index``.
    Changing ``count`` moves mowers between instances (they reconnect on the new owner).
    """

    index: int = 0
    count: int = 1

    def __post_init__(self) -> None:
        if self.count < 1 or not 0 <= self.index < self.count:
            msg = f"invalid shard {self.index} of {self.count}"
            raise ValueError(msg)

    def owns(self, mower_id: UUID) -> bool:
        """True if this shard holds the connection of ``mower_id``."""
        return mower_id.int % self.count == self.index


class ConnectionStatus(StrEnum):
    """Link state of one mower held by the gateway."""

    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"


def command_rejection(
    registration: MowerRegistration | None, envelope: CommandEnvelope
) -> str | None:
    """Why ``envelope`` must not reach the mower, or None if it may.

    The account check is defence in depth: Fleet already checked the permission, but a command
    whose account differs from the mower's registered owner never reaches the controller.
    """
    if registration is None:
        return "unknown mower"
    if registration.mower_id != envelope.mower_id:
        return "command is for another mower"
    if registration.account_id != envelope.account_id:
        return "the command's account does not own this mower"
    return None


def camera_rejection(
    registration: MowerRegistration | None, request: CameraSessionRequest
) -> tuple[CameraSessionErrorCode, str] | None:
    """Why a camera session must not be given, or None if it may (the command account rule)."""
    if registration is None:
        return CameraSessionErrorCode.NOT_FOUND, "unknown mower"
    if registration.account_id != request.account_id:
        return CameraSessionErrorCode.FORBIDDEN, "the request's account does not own this mower"
    return None


def preview_rejection(
    registration: MowerRegistration | None, request: PathPreviewGatewayRequest
) -> tuple[PathPreviewErrorCode, str] | None:
    """Why a path preview must not run, or None if it may (the command account rule)."""
    if registration is None:
        return PathPreviewErrorCode.NOT_FOUND, "unknown mower"
    if registration.account_id != request.account_id:
        return PathPreviewErrorCode.FORBIDDEN, "the request's account does not own this mower"
    return None


def bridge_scan_rejection(
    registration: MowerRegistration | None, request: BridgeScanRequest
) -> tuple[BridgeScanErrorCode, str] | None:
    """Why a bridge scan must not run, or None if it may (the command account rule)."""
    if registration is None:
        return BridgeScanErrorCode.NOT_FOUND, "unknown mower"
    if registration.account_id != request.account_id:
        return BridgeScanErrorCode.FORBIDDEN, "the request's account does not own this mower"
    return None


def firmware_check_rejection(
    registration: MowerRegistration | None, request: FirmwareCheckRequest
) -> tuple[FirmwareCheckErrorCode, str] | None:
    """Why a firmware check must not run, or None if it may (the command account rule)."""
    if registration is None:
        return FirmwareCheckErrorCode.NOT_FOUND, "unknown mower"
    if registration.account_id != request.account_id:
        return FirmwareCheckErrorCode.FORBIDDEN, "the request's account does not own this mower"
    return None


def bridge_host_of(registration: MowerRegistration) -> tuple[str, int] | None:
    """``(host, port)`` of the registration's Bluetooth bridge link, None without one."""
    link = registration.links.get(LINK_BLE_BRIDGE)
    if not link or not isinstance(link.get("host"), str):
        return None
    port = link.get("port", 6053)
    return str(link["host"]).casefold(), int(port) if isinstance(port, int) else 6053


def bluetooth_bridge_of(registration: MowerRegistration) -> tuple[str, int] | None:
    """``(host, port)`` of the bridge a registration reaches its mower through over Bluetooth:
    its ``ble_bridge`` link, or its own options when the connection kind is ``ble_bridge``."""
    linked = bridge_host_of(registration)
    if linked is not None:
        return linked
    if registration.connection_kind is not ConnectionKind.BLE_BRIDGE:
        return None
    options = registration.connection_options
    if not isinstance(options.get("host"), str):
        return None
    port = options.get("port", 6053)
    return str(options["host"]).casefold(), int(port) if isinstance(port, int) else 6053


def holds_bluetooth_through(
    registration: MowerRegistration, state: MowerState | None, host: str, port: int
) -> bool:
    """True when ``registration``'s mower is connected over Bluetooth through the bridge at
    ``host``:``port`` right now (its last state reports the ``ble_bridge`` connection): the
    mower then does not advertise, and a scan through that bridge cannot see it."""
    if state is None or state.connection is not ConnectionKind.BLE_BRIDGE:
        return False
    return bluetooth_bridge_of(registration) == (host.casefold(), port)


def scan_found(reply: BridgeScanReply, expected_name: str | None) -> bool:
    """True when the scan saw the expected mower (any mower without an expected name)."""
    if expected_name is None:
        return bool(reply.devices)
    wanted = expected_name.casefold()
    return any((device.name or "").casefold() == wanted for device in reply.devices)


def bridge_in_use(
    registration: MowerRegistration, state: MowerState | None, host: str, port: int
) -> bool:
    """True when ``registration``'s mower holds an open bridge link to ``host``:``port``
    (its last state reports the link connected): a scan from another mower through that
    bridge would fight the open connection and is answered ``busy``."""
    bridge = bridge_host_of(registration)
    if bridge is None or bridge != (host.casefold(), port):
        return False
    if state is None:
        return False
    link = state.links.get(LINK_BLE_BRIDGE)
    return link is not None and link.connected


def _strip(options: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in options.items() if k not in DRIVE_CALIBRATION_OPTION_KEYS}


def without_calibration(registration: MowerRegistration) -> MowerRegistration:
    """The registration without the stored drive calibration (ADR 0042): the calibration keys
    are MowDeck's, not the adapter's, so they never reach a connection."""
    return registration.model_copy(
        update={
            "connection_options": _strip(registration.connection_options),
            "links": {kind: _strip(link) for kind, link in registration.links.items()},
        }
    )


def drive_calibration_of(registration: MowerRegistration) -> DriveCalibration | None:
    """The drive calibration stored with the mower: on a link (the drive link, e.g. the
    Bluetooth bridge) or in the connection options; the newest one wins."""
    return calibration_of_links({**registration.links, "": registration.connection_options})


def same_link(old: MowerRegistration, new: MowerRegistration) -> bool:
    """True when ``new`` opens the same connection as ``old``: a change of the drive
    calibration alone does not reopen the link (the controller reads it on every run)."""
    before, after = without_calibration(old), without_calibration(new)
    return (before.connection_kind, before.connection_options, before.links) == (
        after.connection_kind,
        after.connection_options,
        after.links,
    )


def links_changed(previous: MowerState | None, current: MowerState) -> bool:
    """True when the additional links differ from the previous state (the capabilities may
    have changed with them: manual driving follows the bridge)."""
    before = previous.links if previous is not None else {}
    if before.keys() != current.links.keys():
        return True
    return any(before[k].connected != current.links[k].connected for k in before)


def path_geometry_key(path: MowPath | None) -> str:
    """What makes a mow path a different one (not its progress): its route and point count."""
    if path is None:
        return "none"
    return f"{path.path_hash}:{path.points_total}:{path.planned_at.isoformat()}"


def path_publish_due(
    published: tuple[str, str] | None,
    path: MowPath | None,
    *,
    since_last_seconds: float,
    progress_interval_seconds: float,
) -> bool:
    """Whether the path snapshot must be published now.

    ``published`` is the geometry key and progress of the last publish (None: nothing published
    on this connection yet, so the first check always publishes, also a cleared path: a value
    left on the stream from before must not outlive the job). A new route or a cleared path is
    published at once; a progress change at most every ``progress_interval_seconds``.
    """
    if published is None:
        return True
    geometry = path_geometry_key(path)
    if geometry != published[0]:
        return True
    progress = path.progress.model_dump_json() if path and path.progress else ""
    if progress == published[1]:
        return False
    return since_last_seconds >= progress_interval_seconds


def is_significant_change(previous: MowerState | None, current: MowerState) -> bool:
    """True if ``current`` differs from ``previous`` beyond the timestamp and adapter extras.

    ``observed_at`` changes with every snapshot and ``raw`` holds adapter specific counters, so
    neither counts as a change; the heartbeat publishes them regularly anyway. ``heartbeat_at``
    and ``connected`` belong to the gateway's link, not to the mower's report (ADR 0040).
    """
    if previous is None:
        return True
    ignored = {"observed_at", "raw", "heartbeat_at", "connected"}
    return previous.model_dump(exclude=ignored) != current.model_dump(exclude=ignored)


def backoff(initial: float, maximum: float, factor: float = 2.0) -> Iterator[float]:
    """Delays ``initial, initial*factor, ...`` capped at ``maximum``, forever."""
    if initial <= 0 or maximum < initial or factor < 1:
        msg = "backoff needs 0 < initial <= maximum and factor >= 1"
        raise ValueError(msg)
    delay = initial
    while True:
        yield delay
        delay = min(delay * factor, maximum)
