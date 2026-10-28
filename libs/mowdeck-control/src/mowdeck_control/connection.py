"""The adapter interface between MowDeck and one mower: :class:`MowerConnection`.

An adapter translates between MowDeck contracts and one way of talking to a mower (simulated,
BLE bridge, vendor cloud, ...). It is deliberately thin: it does not de-duplicate, check expiry,
run the dead-man switch or validate against capabilities. The
:class:`~mowdeck_control.MowerController` does all of that for every adapter in the same way.

Rules for adapters:

* ``execute(EmergencyStop())`` may be called while another call is still running and must act
  immediately (on PyMammotion: the ``EMERGENCY`` priority).
* Safety sensor stops (lift, tilt, collision) are reported as ``MowerActivity.ERROR`` and are
  never cleared by a command; :class:`~mowdeck_control.errors.SensorStop` is raised instead.
* ``watch_state()`` yields the current state first and then every change.
* A stuck mower (wheel slip, trapped) that manual driving may free reports ``error`` with
  ``drive_recoverable = True``; sensor stops never set it.
* Adapters that can take a still image also implement :class:`PhotoCapture`
  (``capture_photo``); callers check with ``isinstance(connection, PhotoCapture)``.

Optional capabilities are separate protocols checked with ``isinstance``: an adapter with a camera
also implements :class:`CameraCapable` (see :func:`camera_session_of`), and one that can tell the
mower to stop publishing its camera stream implements :class:`CameraSessionRelease` (see
:func:`release_camera_session_of`, ADR 0045); an adapter that can rebuild its map from what it
already holds implements :class:`MapRebuildable` (see :func:`current_map_of`); an adapter that
confirms commands through the mower's state implements :class:`TwoPhaseExecution` (see
:func:`dispatch_of`), so the controller can release its queue while the confirmation is pending; an
adapter that knows the route the mower planned for its job implements :class:`MowPathSource` (see
:func:`current_path_of`), and one that can ask the mower to plan a route without starting it
implements :class:`PathPreviewCapable`. An adapter that can make the mower leave its charging
station by itself sets ``MowerCapabilities.undock`` and implements :class:`UndockCapable` (ADR 0041:
a spot run may then start at the dock). An adapter that can read and change the vendor's speed
setting for manual driving declares ``MowerCapabilities.drive_speed_setting``, reports the value in
``MowerState.drive_speed_mps`` and implements :class:`DriveSpeedCapable` (ADR 0042, see
:func:`drive_speed_port_of`). An adapter that asks the mower for its reports while driving
implements :class:`ReportRateCapable`, so a drive calibration can ask for them as fast as the link
allows (ADR 0042, see :func:`report_rate_port_of`). An adapter that can read and change the
mower's own settings declares them in ``MowerCapabilities.settings``, reports their values in
``MowerState.settings`` and implements :class:`SettingsCapable` (ADR 0046, see
:func:`settings_port_of`). An adapter that can restart the mower, read
its map again, make it forget its dock position or read the RTK origin and dock pose again lists
those actions in ``MowerCapabilities.maintenance`` and implements :class:`MaintenanceCapable`
(ADR 0048, see :func:`maintenance_port_of`); ``leave_dock`` there is :class:`UndockCapable`.

Links and firmware (ADR 0038) are optional capabilities as well, declared in the capabilities
and implemented as protocols: an adapter whose connection can consist of several links (the
primary one plus e.g. a Bluetooth bridge) lists the extra kinds in
``MowerCapabilities.links_supported`` and reports each of them in ``MowerState.links``; an
adapter that can ask the manufacturer for firmware updates sets
``MowerCapabilities.firmware_update`` and implements :class:`FirmwarePort` (see
:func:`firmware_port_of`); one that can look for the mower through a Bluetooth bridge implements
:class:`BridgeScanCapable` (see :func:`bridge_scanner_of`). An adapter that reaches the firmware
service of the mower's RTK base station sets ``MowerCapabilities.base_station_firmware_update``
and implements :class:`BaseStationPort` (ADR 0047, see :func:`base_station_port_of`). The safety
rules of a firmware update live in :mod:`mowdeck_control.safety` and are enforced by the
controller for every adapter.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from mowdeck_contracts.v1 import (
    BridgeScanReply,
    CameraSession,
    CommandStatus,
    ConnectionKind,
    EmergencyStop,
    FirmwareInfo,
    FirmwareUpdate,
    MowerCapabilities,
    MowerMap,
    MowerState,
    MowPath,
    PauseMow,
    RelocateDock,
    RestartMower,
    ResumeMow,
    ResyncRtk,
    ReturnToDock,
    SetBladeHeight,
    SetLight,
    SetSpeed,
    SettingValue,
    SpotMow,
    SpotStop,
    StartMow,
    SyncMap,
)

JobCommand = (
    StartMow
    | PauseMow
    | ResumeMow
    | ReturnToDock
    | EmergencyStop
    | SetBladeHeight
    | SetSpeed
    | SetLight
    | FirmwareUpdate
    | SpotMow
    | SpotStop
)
"""Every command except :class:`~mowdeck_contracts.v1.ManualDrive`, which goes through a
:class:`DriveSession`."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What an adapter reports back for one command. The controller wraps it into a
    :class:`~mowdeck_contracts.v1.CommandResult`."""

    status: CommandStatus
    reason: str | None = None

    @classmethod
    def executed(cls, reason: str | None = None) -> "Outcome":
        """The mower confirmed the command."""
        return cls(CommandStatus.EXECUTED, reason)

    @classmethod
    def accepted(cls, reason: str | None = None) -> "Outcome":
        """The command was sent; the mower does not confirm this kind of command."""
        return cls(CommandStatus.ACCEPTED, reason)

    @classmethod
    def rejected(cls, reason: str) -> "Outcome":
        """The mower refused the command before acting."""
        return cls(CommandStatus.REJECTED, reason)

    @classmethod
    def failed(cls, reason: str) -> "Outcome":
        """The command was attempted and did not work."""
        return cls(CommandStatus.FAILED, reason)


ConfirmFn = Callable[[], Awaitable[Outcome]]
"""The confirmation phase of a command: waits for the mower's state, sends nothing."""


@dataclass(frozen=True, slots=True)
class Dispatch:
    """The result of the send phase of a command (:meth:`TwoPhaseExecution.dispatch`).

    Either final (``outcome`` set: rejected, failed, or a command without a confirmation) or
    pending (``confirm`` set): the command went out and ``confirm()`` waits for the mower's state
    to show it. ``sent`` says what went out; the controller uses it when it interrupts the wait.
    """

    outcome: Outcome | None = None
    confirm: ConfirmFn | None = None
    sent: str = "command sent"

    @classmethod
    def done(cls, outcome: Outcome) -> "Dispatch":
        """Nothing left to confirm: ``outcome`` is the result."""
        return cls(outcome=outcome)

    @classmethod
    def awaiting(cls, sent: str, confirm: ConfirmFn) -> "Dispatch":
        """The command was sent (``sent``); ``confirm()`` waits for the mower's state."""
        return cls(confirm=confirm, sent=sent)

    async def settle(self) -> Outcome:
        """The final outcome: ``outcome``, or the result of the confirmation wait."""
        if self.confirm is not None:
            return await self.confirm()
        assert self.outcome is not None  # noqa: S101 - one of both is always set
        return self.outcome


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """Input of a connection factory (see :mod:`mowdeck_control.registry`)."""

    mower_id: UUID
    account_id: UUID
    options: Mapping[str, Any] = field(default_factory=dict)
    """Adapter specific settings, for example a BLE address or a bridge URL."""


@runtime_checkable
class DriveSession(Protocol):
    """An open manual drive session (joystick).

    The session itself has no timer: the controller runs the dead-man switch and calls
    :meth:`stop` and :meth:`close` when frames stop arriving. Adapters must make :meth:`stop`
    and :meth:`close` idempotent and safe to call after the link broke.
    """

    @property
    def is_open(self) -> bool:
        """False after :meth:`close` or after the mower ended the session (sensor stop)."""
        ...

    async def update(self, linear: float, angular: float, blade_on: bool) -> None:
        """Send one frame: ``linear`` and ``angular`` in -1..1, blades on or off.

        Raises :class:`~mowdeck_control.errors.SensorStop` when a safety sensor stopped the
        mower, and :class:`~mowdeck_control.errors.CommandRejected` when the session is closed.
        """
        ...

    async def stop(self) -> None:
        """Stop wheels and blades now; the session stays open."""
        ...

    async def close(self) -> None:
        """Stop and release the session."""
        ...


@runtime_checkable
class FrameDeadlineCapable(Protocol):
    """Optional extension of a :class:`DriveSession` whose link keeps moving the mower on its
    own until it gets a zero frame (the Mammotion cloud remote drive repeats the last speed,
    ADR 0049). After every accepted frame the controller tells the session how long that frame
    may live (its ``ttl_ms``); the session then sends a zero frame itself when no newer frame
    arrived in time. A second layer below the controller's dead-man timer, never instead of it.
    """

    def expect_next_frame_within(self, ttl_s: float) -> None:
        """The last frame expires ``ttl_s`` seconds from now unless another one follows."""
        ...


def frame_deadline_of(session: object) -> FrameDeadlineCapable | None:
    """``session`` as a :class:`FrameDeadlineCapable`, or None when it does not implement it."""
    return session if isinstance(session, FrameDeadlineCapable) else None


@dataclass(frozen=True, slots=True)
class Photo:
    """A still image from (or of) the mower."""

    data: bytes
    media_type: str
    """``image/jpeg`` or ``image/png``."""


@runtime_checkable
class PhotoCapture(Protocol):
    """Optional extension of :class:`MowerConnection`: a still image of the surroundings.

    The Mammotion adapter may implement it once the camera can be read without the vendor app;
    the simulator renders its garden from the map. Raises
    :class:`~mowdeck_control.errors.NotConnected` when the link is down.
    """

    async def capture_photo(self) -> Photo:
        """Take one image (JPEG or PNG)."""
        ...


@runtime_checkable
class MowerConnection(Protocol):
    """One link to one mower."""

    @property
    def kind(self) -> ConnectionKind:
        """How this adapter talks to the mower."""
        ...

    @property
    def is_connected(self) -> bool:
        """True between a successful :meth:`connect` and :meth:`disconnect`."""
        ...

    async def connect(self) -> None:
        """Open the link. Idempotent."""
        ...

    async def disconnect(self) -> None:
        """Close the link and end all ``watch_state()`` iterators. Idempotent."""
        ...

    async def capabilities(self) -> MowerCapabilities:
        """What this mower can do over this link."""
        ...

    async def get_state(self) -> MowerState:
        """The latest known state. Raises :class:`~mowdeck_control.errors.NotConnected`."""
        ...

    def watch_state(self) -> AsyncIterator[MowerState]:
        """Push updates: the current state first, then every change, until disconnect."""
        ...

    async def get_map(self) -> MowerMap:
        """Map geometry in local metres plus its geo reference."""
        ...

    async def execute(self, command: JobCommand) -> Outcome:
        """Run one non-drive command.

        Raises :class:`~mowdeck_control.errors.CommandRejected` (or returns
        ``Outcome.rejected``) when the mower refuses it.
        """
        ...

    async def start_drive_session(self) -> DriveSession:
        """Open a manual drive session. Raises
        :class:`~mowdeck_control.errors.CommandRejected` if driving is not possible now."""
        ...


@runtime_checkable
class TwoPhaseExecution(Protocol):
    """Optional capability of a :class:`MowerConnection`: :meth:`MowerConnection.execute` split
    into a send phase and a confirmation phase.

    ``execute(command)`` must behave like ``await (await dispatch(command)).settle()``. Rules:

    * :meth:`dispatch` sends and returns as soon as the command went out (request/reply
      exchanges that belong to sending, such as a route request, are part of it). It raises
      like ``execute``.
    * ``confirm`` only observes the state: it never sends or re-sends anything, it is safe to
      cancel at any point, and a missing confirmation within its timeout yields
      ``Outcome.accepted`` (the command was sent), never ``failed``.

    The controller holds its queue only during :meth:`dispatch`, so later commands are not
    delayed by a confirmation wait, and it cancels the wait when a later job command supersedes
    the command (the result is then ``accepted``).
    """

    async def dispatch(self, command: JobCommand) -> Dispatch:
        """Send ``command``; see the class rules."""
        ...


async def dispatch_of(connection: "MowerConnection", command: JobCommand) -> Dispatch:
    """The send phase of ``command``: :meth:`TwoPhaseExecution.dispatch` when ``connection``
    implements it, otherwise the whole :meth:`MowerConnection.execute` as a final result."""
    if isinstance(connection, TwoPhaseExecution):
        return await connection.dispatch(command)
    return Dispatch.done(await connection.execute(command))


@runtime_checkable
class CameraCapable(Protocol):
    """Optional capability of a :class:`MowerConnection`: short-lived live camera sessions.

    The adapter returns credentials for a viewer to join the vendor's video stream itself
    (ADR 0032); MowDeck never relays media. ``None`` means this connection has no camera
    (for example the BLE bridge of a camera mower). Raises
    :class:`~mowdeck_control.errors.NotConnected` when the link or the vendor cloud is down.
    The session holds a secret token: never log it.
    """

    async def get_camera_session(self) -> CameraSession | None:
        """A fresh camera session, or None without a camera on this connection."""
        ...


async def camera_session_of(connection: object) -> CameraSession | None:
    """The camera session of ``connection``; None when it does not implement
    :class:`CameraCapable` or has no camera."""
    if not isinstance(connection, CameraCapable):
        return None
    return await connection.get_camera_session()


@runtime_checkable
class CameraSessionRelease(Protocol):
    """Optional capability of a :class:`MowerConnection`: tell the mower to stop publishing its
    camera stream (ADR 0045).

    Some mowers keep publishing after the last viewer left, which costs mobile data and battery
    until they are told to stop. The camera relay calls this once it left the source session.
    Rules:

    * Idempotent and safe without a running stream: releasing twice, or before any session was
      requested, sends at most a harmless stop and never raises for that.
    * ``channel`` is the :attr:`~mowdeck_contracts.v1.CameraSession.channel` of the session to
      release when the caller knows it; adapters that stop the stream per mower ignore it, and
      None releases whatever the mower publishes.
    * Raises :class:`~mowdeck_control.errors.NotConnected` when the stop could not be sent (the
      link or the vendor cloud is down, the vendor refused); never anything else for a vendor
      failure.
    """

    async def release_camera_session(self, channel: str | None = None) -> None:
        """Tell the mower to stop publishing its camera stream."""
        ...


async def release_camera_session_of(connection: object, channel: str | None = None) -> bool:
    """Release the camera stream of ``connection``; False (nothing sent) when it does not
    implement :class:`CameraSessionRelease`, True once the stop went out."""
    if not isinstance(connection, CameraSessionRelease):
        return False
    await connection.release_camera_session(channel)
    return True


@runtime_checkable
class MapRebuildable(Protocol):
    """Optional capability of a :class:`MowerConnection`: the map rebuilt from what the adapter
    already holds, without talking to the mower.

    Parts of a map (the dock, the geo reference) can arrive after :meth:`MowerConnection.get_map`
    returned; this lets the Device-Gateway pick them up cheaply on its snapshot recheck, while a
    full re-read (``get_map``) stays reserved for when zones may have changed.
    """

    def current_map(self) -> MowerMap | None:
        """The map from the adapter's current data; None before ``get_map`` loaded one."""
        ...


def current_map_of(connection: object) -> MowerMap | None:
    """The rebuilt map of ``connection``; None when it does not implement
    :class:`MapRebuildable` or has no map yet."""
    if not isinstance(connection, MapRebuildable):
        return None
    return connection.current_map()


@runtime_checkable
class MowPathSource(Protocol):
    """Optional capability of a :class:`MowerConnection`: the route the mower planned for its
    running job, with the progress along it.

    The adapter fetches the route in the background once a job runs (never blocking a command)
    and keeps it until the job ended. :meth:`current_path` only reads what the adapter holds:
    no traffic to the mower, cheap enough to call on every state.
    """

    def current_path(self) -> MowPath | None:
        """The path of the running job with fresh progress; None without a job or before the
        route arrived."""
        ...


def current_path_of(connection: object) -> MowPath | None:
    """The path of the running job of ``connection``; None when it does not implement
    :class:`MowPathSource` or has no path."""
    if not isinstance(connection, MowPathSource):
        return None
    return connection.current_path()


@runtime_checkable
class PathPreviewCapable(Protocol):
    """Optional capability of a :class:`MowerConnection`: ask the mower to plan the route of a
    start (areas and pattern as in :class:`~mowdeck_contracts.v1.StartMow`) without starting it.

    Raises :class:`~mowdeck_control.errors.CommandRejected` while a job runs or for areas or a
    pattern the mower cannot run, :class:`~mowdeck_control.errors.NotConnected` without a link
    and :class:`~mowdeck_control.errors.PathUnavailable` when the route did not arrive (the
    adapter bounds the wait itself).
    """

    async def preview_path(
        self, area_ids: Sequence[str], pattern: Mapping[str, Any] | None
    ) -> MowPath:
        """The route the mower planned (``progress`` is None)."""
        ...


@runtime_checkable
class UndockCapable(Protocol):
    """Optional capability of a :class:`MowerConnection`, declared with
    ``MowerCapabilities.undock`` (ADR 0041): the mower leaves its charging station by itself and
    stops a short way in front of it, then reports ``idle``.

    Only the send: :meth:`leave_dock` returns once the command went out (it does not wait for
    the mower); the spot run watches the state. Raises
    :class:`~mowdeck_control.errors.CommandRejected` when the mower cannot leave now and
    :class:`~mowdeck_control.errors.NotConnected` without a link.
    """

    async def leave_dock(self) -> None:
        """Send the mower out of its charging station."""
        ...


@runtime_checkable
class DriveSpeedCapable(Protocol):
    """Optional capability of a :class:`MowerConnection`, declared with
    ``MowerCapabilities.drive_speed_setting`` (ADR 0042): the vendor's speed setting for manual
    driving, by which the mower scales the stick. It is a setting, not a drive frame: adapters
    send it over their command channel (for a dual link the cloud).

    Both raise :class:`~mowdeck_control.errors.NotConnected` without a link and
    :class:`~mowdeck_control.errors.CommandRejected` when the mower refuses.
    """

    async def get_drive_speed(self) -> float | None:
        """Ask the mower for the current setting (m/s); None when it does not answer. The
        adapter also reports the value in ``MowerState.drive_speed_mps``."""
        ...

    async def set_drive_speed(self, speed_mps: float) -> None:
        """Change the setting (the controller checked the range)."""
        ...


MaintenanceCommand = RestartMower | SyncMap | RelocateDock | ResyncRtk
"""The maintenance commands an adapter runs through :class:`MaintenanceCapable`
(``leave_dock`` goes through :class:`UndockCapable`)."""


@runtime_checkable
class MaintenanceCapable(Protocol):
    """Optional capability of a :class:`MowerConnection`, declared with
    ``MowerCapabilities.maintenance`` (ADR 0048): one-shot device actions. The controller checked
    the capability and the safety rules (:func:`mowdeck_control.safety.maintenance_problem`).
    Rules:

    * State-changing actions (``restart_mower``, ``relocate_dock``) are sent once and never
      re-sent without a reply; the result is ``accepted`` (sent), the mower does not confirm.
    * ``sync_map`` reads the whole map again even when the adapter holds a fresh one, and
      returns once it arrived (``executed``) or its own timeout passed (``accepted``); the next
      :meth:`MowerConnection.get_map` returns the new map without reading it once more.
    * ``resync_rtk`` asks for the RTK origin and the dock pose; ``executed`` when the answer
      arrived within the adapter's wait, ``accepted`` otherwise.
    * Raises :class:`~mowdeck_control.errors.NotConnected` without a link and
      :class:`~mowdeck_control.errors.CommandRejected` (or returns ``Outcome.rejected``) when
      the mower cannot do it now.
    """

    async def run_maintenance(self, command: MaintenanceCommand) -> Outcome:
        """Run ``command`` (any of :data:`MaintenanceCommand`)."""
        ...


def maintenance_port_of(connection: object) -> MaintenanceCapable | None:
    """``connection`` as a :class:`MaintenanceCapable`, or None when it does not implement it."""
    return connection if isinstance(connection, MaintenanceCapable) else None


def drive_speed_port_of(connection: object) -> DriveSpeedCapable | None:
    """``connection`` as a :class:`DriveSpeedCapable`, or None when it does not implement it."""
    return connection if isinstance(connection, DriveSpeedCapable) else None


@runtime_checkable
class SettingsCapable(Protocol):
    """Optional capability of a :class:`MowerConnection`, declared with
    ``MowerCapabilities.settings`` (ADR 0046): the mower's own settings (rain sensor, battery
    levels, lights, voice, non-work hours, ...).

    The adapter reports the current values in ``MowerState.settings`` and changes one with
    :meth:`set_setting`. Rules:

    * Only declared keys arrive, with a value the controller checked against the declaration
      (:meth:`~mowdeck_contracts.v1.SettingSpec.value_problem`).
    * A setting never makes the mower move: an adapter declares no key whose change starts the
      wheels or the blades.
    * The outcome is ``executed`` when the mower confirmed the new value (a read back), else
      ``accepted`` (sent, not confirmed).

    Raises :class:`~mowdeck_control.errors.NotConnected` without a link and
    :class:`~mowdeck_control.errors.CommandRejected` when the mower refuses.
    """

    async def set_setting(self, key: str, value: SettingValue) -> Outcome:
        """Change setting ``key`` to ``value``."""
        ...


def settings_port_of(connection: object) -> SettingsCapable | None:
    """``connection`` as a :class:`SettingsCapable`, or None when it does not implement it."""
    return connection if isinstance(connection, SettingsCapable) else None


@runtime_checkable
class ReportRateCapable(Protocol):
    """Optional capability of a :class:`MowerConnection` whose mower reports only when asked
    (ADR 0042): while a drive session is open the adapter asks for a fresh report whenever the
    last one is older than its drive interval (about a second by default). A drive calibration
    measures from these reports and asks for them faster while it runs."""

    def set_drive_report_interval(self, interval_s: float | None) -> None:
        """Ask for reports while driving at most every ``interval_s`` seconds (the adapter
        clamps it to what its link allows); None restores the adapter's default."""
        ...


def report_rate_port_of(connection: object) -> ReportRateCapable | None:
    """``connection`` as a :class:`ReportRateCapable`, or None when it does not implement it."""
    return connection if isinstance(connection, ReportRateCapable) else None


@runtime_checkable
class TrackingRateCapable(Protocol):
    """Optional capability of a :class:`MowerConnection` whose mower reports only when asked
    (ADR 0058): while the theft tracking mode holds a lease, the adapter asks for a report every
    ``interval_s`` seconds, whatever the mower's activity, and backs off when the mower stays
    silent. A connection whose mower reports on its own (the simulator) needs no such port."""

    def set_tracking_interval(self, interval_s: float | None) -> float | None:
        """Ask for reports at most every ``interval_s`` seconds (the adapter clamps it to what
        its link allows) and return the interval it applies; None restores the normal rhythm
        and returns None."""
        ...


def tracking_rate_port_of(connection: object) -> TrackingRateCapable | None:
    """``connection`` as a :class:`TrackingRateCapable`, or None when it does not implement it."""
    return connection if isinstance(connection, TrackingRateCapable) else None


@runtime_checkable
class FirmwarePort(Protocol):
    """Optional capability of a :class:`MowerConnection`: the manufacturer's firmware service
    (ADR 0038), declared with ``MowerCapabilities.firmware_update``.

    The adapter only supplies the vendor calls and keeps the resulting
    :class:`~mowdeck_contracts.v1.FirmwareInfo` in ``MowerState.firmware_info``; the safety
    rules of an update (:func:`mowdeck_control.safety.firmware_update_problem`) and the lock-out
    while it runs are the controller's. Both methods raise
    :class:`~mowdeck_control.errors.NotConnected` without a usable link to the vendor.
    """

    async def check_firmware(self) -> FirmwareInfo | None:
        """Ask the vendor what is installed and what is offered; None when it cannot say."""
        ...

    async def start_firmware_update(self, version: str) -> None:
        """Ask the vendor to install ``version`` (the controller checked the preconditions).

        Raises :class:`~mowdeck_control.errors.CommandRejected` when the vendor refuses.
        """
        ...


def firmware_port_of(connection: object) -> FirmwarePort | None:
    """``connection`` as a :class:`FirmwarePort`, or None when it does not implement it."""
    return connection if isinstance(connection, FirmwarePort) else None


@runtime_checkable
class BaseStationPort(Protocol):
    """Optional capability of a :class:`MowerConnection`: the firmware of the RTK base station
    the mower is linked to (ADR 0047), declared with
    ``MowerCapabilities.base_station_firmware_update``.

    The base station has no connection of its own: the mower's connection asks the
    manufacturer's firmware service with the base station's id (the connection option
    ``base_station``) and keeps the answer in ``MowerState.base_station.firmware_info``. The
    rules of an update (:func:`mowdeck_control.safety.base_station_firmware_update_problem`) are
    the controller's. Both methods raise :class:`~mowdeck_control.errors.NotConnected` without a
    usable link to the vendor.
    """

    async def check_base_station_firmware(self) -> FirmwareInfo | None:
        """Ask the vendor what the base station runs and what is offered; None when the mower
        has no base station or the vendor cannot say."""
        ...

    async def start_base_station_firmware_update(self, version: str) -> None:
        """Ask the vendor to install ``version`` on the base station (the controller checked the
        preconditions). Raises :class:`~mowdeck_control.errors.CommandRejected` when the vendor
        refuses."""
        ...


def base_station_port_of(connection: object) -> BaseStationPort | None:
    """``connection`` as a :class:`BaseStationPort`, or None when it does not implement it."""
    return connection if isinstance(connection, BaseStationPort) else None


@runtime_checkable
class BridgeScanCapable(Protocol):
    """Optional capability of a :class:`MowerConnection`: look for the mower through a
    Bluetooth bridge (ADR 0038), offered when ``ble_bridge`` is in
    ``MowerCapabilities.links_supported``.

    The result is neutral: the bridge's own description
    (:class:`~mowdeck_contracts.v1.ProxyInfo`), the mower advertisements it saw
    (:class:`~mowdeck_contracts.v1.ScannedDevice`) and an error code when the bridge could not
    be used. A connection that already holds an open link through the same bridge answers from
    its live scanner instead of opening a second connection to it.
    """

    async def scan_bridge(
        self,
        *,
        host: str,
        port: int,
        secret: str,
        expected_name: str | None,
        timeout_s: float,
    ) -> BridgeScanReply:
        """Scan through the bridge at ``host``:``port`` (``secret``: its API key) until
        ``expected_name`` advertises or ``timeout_s`` passes; never raises for bridge errors
        (they come back as ``error``)."""
        ...


def bridge_scanner_of(connection: object) -> BridgeScanCapable | None:
    """``connection`` as a :class:`BridgeScanCapable`, or None when it does not implement it."""
    return connection if isinstance(connection, BridgeScanCapable) else None
