"""One mower held by the gateway: its connection, controller, telemetry and commands.

A :class:`MowerSession` supervises the link: it creates the connection through the
:class:`~device_gateway.application.ports.ConnectionProvider`, wraps it in a
:class:`~mowdeck_control.MowerController`, publishes telemetry and connection events, and
reconnects with backoff when the link drops. Commands run as one task each; the controller
serializes ordinary commands of this mower and lets an emergency stop overtake them, and no
mower ever waits for another one.

Spot runs (ADR 0039): a ``spot_mow`` is a MowDeck job, not the mower's. The controller starts
the :class:`~mowdeck_control.SpotMowRun` and answers ``accepted`` at once, which is published as
usual; a follow-up task waits for the run's end and publishes the final ``executed`` (the
plan was driven) or ``failed`` result with the run's summary as the reason. The run status
reaches clients in ``MowerState.spot_run`` (the controller merges it into every state) and the
plan with its progress as the ordinary path snapshot (``controller.current_path()``). A lost
link or a released mower closes the controller, which ends the run (``link_lost``) and so
publishes its final result too.

Drive calibration (ADR 0042): a ``drive_calibrate`` runs through the controller the same way
(``accepted`` at once, then ``executed`` with the fit or ``failed`` with the reason); its status
and result reach clients in ``MowerState.drive_calibration``. The stored calibration lives in the
registration's link options (``DRIVE_CALIBRATION_OPTION_KEYS``): the session strips those keys
before the connection is created and hands them to the controller, which applies them to every
spot run; a change of only the calibration updates the registration without reconnecting.

Link heartbeat (ADR 0040): every state the session publishes carries ``heartbeat_at``, the time
the gateway last knew the link alive (``MowerConnection.is_connected``), and ``connected``. While
the link is up the latest state is republished every ``heartbeat_seconds`` even when the adapter
reported nothing new, with a fresh ``heartbeat_at`` and the unchanged ``observed_at``; clients
judge online and offline by ``heartbeat_at``, not by the age of the values. When the link ends
the heartbeats stop and the last state goes out once more with ``connected = false``.
"""

import asyncio
import contextlib
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import (
    MOWER_CONNECTED,
    MOWER_DISCONNECTED,
    PHOTO_MAX_BYTES,
    BridgeScanReply,
    CameraAccess,
    CameraSession,
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    DriveCalibrate,
    FirmwareInfo,
    MowerActivity,
    MowerCapabilitiesSnapshot,
    MowerConnectionEvent,
    MowerMapSnapshot,
    MowerPathSnapshot,
    MowerRegistration,
    MowerState,
    MowPath,
    SpotMow,
    VisionDemand,
    utc_now,
)
from mowdeck_control import (
    MowerConnection,
    MowerController,
    NotConnected,
    Photo,
    PhotoCapture,
    PhotoUnavailable,
    bridge_scanner_of,
    camera_session_of,
    firmware_port_of,
    release_camera_session_of,
)
from mowdeck_service_kit import get_logger
from pydantic import BaseModel

from device_gateway.application.ports import (
    CameraFrame,
    CameraFrames,
    CameraSnapshotter,
    Clock,
    ConnectionProvider,
    CredentialSource,
    FrameSetSnapshotter,
    GatewayPublisher,
    NoCredentials,
    SnapshotFailed,
    SnapshotTarget,
    SnapshotUnavailable,
)
from device_gateway.domain.rules import (
    ConnectionStatus,
    backoff,
    drive_calibration_of,
    is_significant_change,
    links_changed,
    path_geometry_key,
    path_publish_due,
    without_calibration,
)

_log = get_logger(__name__)

_JOB_ACTIVITIES = frozenset({MowerActivity.MOWING, MowerActivity.PAUSED, MowerActivity.RETURNING})
_AT_REST = frozenset({MowerActivity.DOCKED, MowerActivity.CHARGING, MowerActivity.IDLE})


def fingerprint(model: BaseModel) -> str:
    """A short, stable hash of a model's JSON (change detection and msg id)."""
    return hashlib.sha256(model.model_dump_json().encode("utf-8")).hexdigest()[:32]


def _without_markers(options: Mapping[str, Any]) -> dict[str, Any]:
    """The options without Fleet's ``credentials`` markers (the secrets take their place)."""
    return {k: v for k, v in options.items() if not k.startswith("credentials")}


@dataclass(frozen=True, slots=True)
class SessionTiming:
    """Timing of one session (from the settings)."""

    heartbeat_seconds: float = 10.0
    """The latest state is republished this often while the link is alive (link heartbeat,
    ADR 0040), also when the adapter reported nothing new."""
    drive_heartbeat_seconds: float = 1.0
    """Heartbeat while a drive session is open: automated driving (AI assist) aborts when no
    state arrives for a few seconds, also when the mower does not move (stuck)."""
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    connection_check_seconds: float = 2.0
    snapshot_recheck_seconds: float = 30.0
    """Capabilities and map are compared again (and republished when changed) this often
    while states arrive: a mower learns its model, limits or dock only after connecting."""
    path_progress_seconds: float = 5.0
    """A mow path whose only change is the progress is republished at most this often (a new
    route or a cleared one at once)."""
    firmware_check_initial_seconds: float = 120.0
    """Wait after a connect before the manufacturer is asked for firmware updates (after the
    adapter's device information rounds); 0 switches the periodic check off."""
    firmware_check_interval_seconds: float = 24 * 3600.0
    """Then ask again this often while the connection lasts."""
    close_timeout_seconds: float = 30.0
    """Longest wait for a connection to close (its link, a bridge's proxy) before the session
    gives up on it and logs a warning."""


class ConnectionLost(Exception):  # noqa: N818 - reads as an event
    """The link of an established connection dropped."""


class PhotoUnsupported(Exception):  # noqa: N818 - reads as a state
    """The adapter of the mower cannot take photos."""


class PathPreviewUnsupported(Exception):  # noqa: N818 - reads as a state
    """The adapter of the mower cannot plan a route without starting."""


class FirmwareUnsupported(Exception):  # noqa: N818 - reads as a state
    """The connection of the mower cannot ask the manufacturer for firmware (no cloud link)."""


class BridgeScanUnsupported(Exception):  # noqa: N818 - reads as a state
    """The connection of the mower cannot look through a Bluetooth bridge."""


def _cancelling() -> bool:
    """True when the current task was asked to stop (``Task.cancel``), as opposed to a
    ``CancelledError`` that escaped from a library's own future."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def snapshot_camera_uid(session: CameraSession) -> int | None:
    """The camera a snapshot shows: the first camera with a publisher uid (the front or left
    one on Mammotion mowers, slot 0, uid 1); None (the first publisher) without one."""
    return next((camera.uid for camera in session.cameras if camera.uid is not None), None)


class MowerSession:
    """Supervises the connection of one mower and executes its commands."""

    def __init__(
        self,
        registration: MowerRegistration,
        *,
        provider: ConnectionProvider,
        publisher: GatewayPublisher,
        timing: SessionTiming,
        clock: Clock = utc_now,
        credentials: CredentialSource | None = None,
        snapshotters: Sequence[CameraSnapshotter] = (),
        snapshot_timeout_seconds: float = 20.0,
    ) -> None:
        self.registration = registration
        self._snapshotters = tuple(snapshotters)
        self._snapshot_timeout_seconds = snapshot_timeout_seconds
        self._photo_lock = asyncio.Lock()
        self._credentials: CredentialSource = credentials or NoCredentials()
        self.status = ConnectionStatus.CONNECTING
        self.connected_since: datetime | None = None
        self.last_activity_at: datetime | None = None
        self.last_state: MowerState | None = None
        self.heartbeat_at: datetime | None = None
        """When the link was last known alive (the ``heartbeat_at`` of the last publish)."""
        self._provider = provider
        self._publisher = publisher
        self._timing = timing
        self._clock = clock
        self._controller: MowerController | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._commands: set[asyncio.Task[CommandResult]] = set()
        self._spot_followups: set[asyncio.Task[None]] = set()
        """Tasks that publish the final result of a spot run (not part of :meth:`wait_idle`:
        a run lasts minutes; closing the controller ends it)."""
        self._snapshot_tasks: set[asyncio.Task[None]] = set()
        self._recheck_again: MowerController | None = None
        self._published_map: str | None = None
        self._published_capabilities: str | None = None
        self._published_path: tuple[str, str] | None = None
        """Geometry key and progress of the last path snapshot of this connection."""
        self._path_published_at = 0.0
        self._path_lock = asyncio.Lock()

    @property
    def mower_id(self) -> UUID:
        """The mower this session holds."""
        return self.registration.mower_id

    @property
    def in_flight(self) -> int:
        """Number of commands not yet answered."""
        return len(self._commands)

    async def capture_photo(self, demand: VisionDemand = VisionDemand.ROUTINE) -> Photo:
        """A still image, one at a time per mower, from the first source that has one:

        1. the adapter itself (:class:`~mowdeck_control.PhotoCapture`, the simulator's garden),
           unless it raises :class:`~mowdeck_control.PhotoUnavailable`;
        2. the camera relay (ADR 0045), when configured: a frame of the session it holds or
           starts for this connection's account and mower (no second vendor session while
           someone watches), within the Vision limits;
        3. the deprecated capture worker (ADR 0035), when configured, with a fresh vendor
           session of the connection.

        A source that cannot serve the mower at all (:class:`SnapshotUnavailable`: not
        reachable, switched off, no driver for the camera's provider) hands over to the next;
        any other failure (a limit, an offline mower, no frame in time) is final, so a refusal
        of the relay is never bypassed through the worker.

        Raises :class:`~mowdeck_control.NotConnected` while the link is down,
        :class:`PhotoUnsupported` when no source applies (no camera, nothing configured) and
        :class:`~device_gateway.application.ports.SnapshotFailed` when the sources gave no
        frame.
        """
        found = await self.capture_frames(demand)
        return found.frames[0].photo

    async def capture_frames(
        self,
        demand: VisionDemand = VisionDemand.ROUTINE,
        *,
        max_frames: int = 1,
        max_bytes: int = PHOTO_MAX_BYTES,
    ) -> CameraFrames:
        """Stills of the same moment from the sources of :meth:`capture_photo`, in the same
        order and with the same hand-over rules.

        With ``max_frames`` above 1 a source that takes frame sets (the camera relay:
        :class:`FrameSetSnapshotter`) gives one frame of each front camera, left first, with
        side and capture time, each at most ``max_bytes / max_frames`` so the set stays
        within ``max_bytes``; every other source (the adapter's own photo, the capture worker)
        gives its single frame. The camera the app shows is no filter: a set always asks for
        both front cameras.
        """
        controller = self._controller
        if controller is None:
            raise NotConnected("the mower is not connected")
        connection = controller.connection
        if isinstance(connection, PhotoCapture):
            try:
                return CameraFrames(frames=(CameraFrame(await connection.capture_photo()),))
            except PhotoUnavailable as exc:
                _log.debug("adapter takes no photo", mower_id=str(self.mower_id), reason=str(exc))
        if not self._snapshotters:
            raise PhotoUnsupported("this connection cannot take photos")
        async with self._photo_lock:
            return await self._camera_frames(
                controller, demand, max_frames=max_frames, max_bytes=max_bytes
            )

    async def _camera_frames(
        self,
        controller: MowerController,
        demand: VisionDemand,
        *,
        max_frames: int,
        max_bytes: int,
    ) -> CameraFrames:
        connection = controller.connection
        capabilities = await controller.capabilities()
        if capabilities.camera is CameraAccess.NONE:
            raise PhotoUnsupported("this connection has no camera")
        vendor: CameraSession | None = None
        fetched = False
        handed_over: SnapshotFailed | None = None
        for snapshotter in self._snapshotters:
            target = SnapshotTarget(
                account_id=self.registration.account_id, mower_id=self.mower_id, demand=demand
            )
            if snapshotter.needs_camera_session:
                if not fetched:
                    vendor = await camera_session_of(connection)
                    fetched = True
                if vendor is None:
                    continue
                target = replace(target, session=vendor, camera_uid=snapshot_camera_uid(vendor))
            try:
                if max_frames > 1 and isinstance(snapshotter, FrameSetSnapshotter):
                    found = await snapshotter.frame_set(
                        target,
                        max_frames=max_frames,
                        max_bytes=max_bytes // max_frames,
                        timeout_seconds=self._snapshot_timeout_seconds,
                    )
                else:
                    photo = await snapshotter.snapshot(
                        target, timeout_seconds=self._snapshot_timeout_seconds
                    )
                    found = CameraFrames(frames=(CameraFrame(photo),))
            except SnapshotUnavailable as exc:
                _log.info(
                    "camera frame source unavailable, trying the next",
                    mower_id=str(self.mower_id),
                    source=type(snapshotter).__name__,
                    detail=str(exc),
                )
                handed_over = exc
                continue
            _log.info(
                "camera frame captured",
                mower_id=str(self.mower_id),
                source=type(snapshotter).__name__,
                camera_uid=target.camera_uid,
                frames=len(found.frames),
                sides=[frame.side for frame in found.frames],
                missing=list(found.missing),
            )
            return found
        if handed_over is not None:
            raise handed_over
        raise PhotoUnsupported("this connection has no camera")

    # --- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        """Start supervising the connection in the background."""
        if self._supervisor is None:
            self._supervisor = asyncio.create_task(self._supervise(), name=f"mower-{self.mower_id}")

    async def wait_idle(self) -> None:
        """Wait until every submitted command has published its result."""
        while self._commands:
            await asyncio.gather(*list(self._commands), return_exceptions=True)

    async def close(self) -> None:
        """Answer in-flight commands, then close the controller and publish the disconnect."""
        await self.wait_idle()
        for task in list(self._snapshot_tasks):
            task.cancel()
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
        if self._spot_followups:  # the closed controller ended the runs: publish their end
            await asyncio.wait(list(self._spot_followups), timeout=5.0)
        self.status = ConnectionStatus.CLOSED

    async def _supervise(self) -> None:
        delays = backoff(self._timing.reconnect_initial_seconds, self._timing.reconnect_max_seconds)
        while True:
            try:
                await self._connect_and_run()
            except asyncio.CancelledError:
                if _cancelling():
                    raise
                # A vendor library cancelled one of its own futures (a dropped transport): a
                # failed connection, not the gateway releasing the mower.
                _log.warning(
                    "mower connection failed",
                    mower_id=str(self.mower_id),
                    error="CancelledError inside the connection",
                )
            except ConnectionLost:
                _log.warning("mower connection lost", mower_id=str(self.mower_id))
                delays = backoff(
                    self._timing.reconnect_initial_seconds, self._timing.reconnect_max_seconds
                )
            except Exception as exc:
                _log.warning(
                    "mower connection failed", mower_id=str(self.mower_id), error=repr(exc)
                )
            self.status = ConnectionStatus.RECONNECTING
            delay = next(delays)
            _log.info("reconnecting mower", mower_id=str(self.mower_id), delay_seconds=delay)
            await asyncio.sleep(delay)

    async def _connected_registration(self) -> MowerRegistration:
        """The registration with the secrets Fleet withheld, fetched for this connect only
        (ADR 0034): every (re)connect asks again, nothing is kept on the session. The secrets
        of the additional links (ADR 0038) are merged into their link the same way."""
        registration = self.registration
        if not registration.needs_credentials:
            return registration
        secrets = await self._credentials.fetch(registration)
        options = _without_markers(registration.connection_options)
        options.update(secrets.options)
        links = {
            kind: {**_without_markers(link), **secrets.link_secrets.get(kind, {})}
            for kind, link in registration.links.items()
        }
        return registration.model_copy(update={"connection_options": options, "links": links})

    async def _connect_and_run(self) -> None:
        registration = without_calibration(await self._connected_registration())
        connection = self._provider.create(registration)
        controller = MowerController(
            connection,
            mower_id=self.mower_id,
            # read on every spot run, so a calibration stored meanwhile applies at once
            drive_calibration=lambda: drive_calibration_of(self.registration),
        )
        started = False
        reason = "connection closed"
        try:
            await controller.start()
            started = True
            self._controller = controller
            self.status = ConnectionStatus.CONNECTED
            self.connected_since = self._clock()
            _log.info("mower connected", mower_id=str(self.mower_id), kind=str(connection.kind))
            await self._connection_event(MOWER_CONNECTED, None)
            await self._publish_snapshots(controller, refresh_map=False)
            self._published_path = None
            await self._publish_path(controller)
            await self._run_connected(controller, connection)
            reason = "connection lost"
            raise ConnectionLost
        except asyncio.CancelledError:
            reason = "gateway released the mower" if _cancelling() else "connection failed"
            raise
        finally:
            self._controller = None
            self.connected_since = None
            if started:  # before the close, which may take a while: clients learn it at once
                await self._publish_disconnected(controller.last_state or self.last_state)
            await self._close_to_the_end(controller, reason)
            if started:
                await self._connection_event(MOWER_DISCONNECTED, reason)

    async def _close_to_the_end(self, controller: MowerController, reason: str) -> None:
        """Close ``controller`` and its connection even when the session is cancelled while
        it closes (a removal during a reconnect): an interrupted close would leave the link,
        and a bridge's proxy and Bluetooth connection, open until the gateway restarts. A
        cancellation that arrived meanwhile is raised once the close is done."""
        closing = asyncio.create_task(
            self._close_controller(controller, reason), name=f"close-{self.mower_id}"
        )
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _close_controller(self, controller: MowerController, reason: str) -> None:
        try:
            async with asyncio.timeout(self._timing.close_timeout_seconds):
                await controller.close()
        except Exception as exc:
            _log.warning(
                "closing the mower connection failed",
                mower_id=str(self.mower_id),
                reason=reason,
                error=repr(exc),
            )
        else:
            _log.info("connection for mower closed", mower_id=str(self.mower_id), reason=reason)

    async def _run_connected(
        self, controller: MowerController, connection: MowerConnection
    ) -> None:
        """Run telemetry, the liveness check and adapter jobs; return when one of them ends."""
        tasks = [
            asyncio.create_task(self._telemetry(controller)),
            asyncio.create_task(self._watch_link(connection)),
            asyncio.create_task(self._firmware_checks(controller)),
            *(asyncio.create_task(job()) for job in self._provider.background_jobs(connection)),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    _log.warning(
                        "mower background job failed",
                        mower_id=str(self.mower_id),
                        error=repr(task.exception()),
                    )
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _firmware_checks(self, controller: MowerController) -> None:
        """Ask the manufacturer for firmware updates once after the connect (when the adapter's
        device information rounds are through) and then daily (ADR 0038); nothing for a
        connection without the capability. The adapter publishes the result in the state."""
        initial = self._timing.firmware_check_initial_seconds
        if initial <= 0:
            return
        await asyncio.sleep(initial)
        while True:
            try:
                await self._check_firmware(controller)
            except (NotConnected, FirmwareUnsupported) as exc:
                _log.info(
                    "periodic firmware check skipped", mower_id=str(self.mower_id), reason=str(exc)
                )
            except Exception as exc:
                _log.warning(
                    "periodic firmware check failed", mower_id=str(self.mower_id), error=repr(exc)
                )
            await asyncio.sleep(self._timing.firmware_check_interval_seconds)

    async def _check_firmware(self, controller: MowerController) -> FirmwareInfo | None:
        capabilities = await controller.capabilities()
        port = firmware_port_of(controller.connection)
        if port is None or not capabilities.firmware_update:
            raise FirmwareUnsupported("this connection cannot ask the manufacturer for firmware")
        info = await port.check_firmware()
        latest = controller.last_state
        if latest is not None and latest.firmware_info != info:
            latest = await controller.connection.get_state()
            await self._publish_state(latest)
            self.last_state = latest
        return info

    async def check_firmware(self) -> FirmwareInfo | None:
        """Ask the manufacturer now (``gateway.mower.<id>.firmware.check``) and publish the
        state that carries the answer.

        Raises :class:`~mowdeck_control.NotConnected` while the mower is not connected (or the
        vendor did not answer) and :class:`FirmwareUnsupported` without the capability.
        """
        controller = self._controller
        if controller is None:
            raise NotConnected("the mower is not connected")
        return await self._check_firmware(controller)

    async def scan_bridge(
        self,
        *,
        host: str,
        port: int,
        secret: str,
        expected_name: str | None,
        timeout_s: float,
    ) -> BridgeScanReply:
        """Look for the mower through a Bluetooth bridge (``gateway.mower.<id>.bridge.scan``).

        Raises :class:`~mowdeck_control.NotConnected` while the mower is not connected and
        :class:`BridgeScanUnsupported` when the connection cannot open bridge links.
        """
        controller = self._controller
        if controller is None:
            raise NotConnected("the mower is not connected")
        capabilities = await controller.capabilities()
        scanner = bridge_scanner_of(controller.connection)
        if scanner is None or "ble_bridge" not in capabilities.links_supported:
            raise BridgeScanUnsupported("this connection cannot look through a Bluetooth bridge")
        return await scanner.scan_bridge(
            host=host, port=port, secret=secret, expected_name=expected_name, timeout_s=timeout_s
        )

    async def _watch_link(self, connection: MowerConnection) -> None:
        # The connection protocol has no "link lost" event, so the flag is polled.
        while connection.is_connected:  # noqa: ASYNC110
            await asyncio.sleep(self._timing.connection_check_seconds)

    async def _telemetry(self, controller: MowerController) -> None:
        """Publish every significant state change, and the latest state as a heartbeat (every
        ``drive_heartbeat_seconds`` while a drive session is open).

        The heartbeat is the link heartbeat of ADR 0040: it goes out while the connection says
        it is connected, even when no new report arrived, and is skipped once it says not."""
        loop = asyncio.get_running_loop()
        drive_heartbeat = min(self._timing.drive_heartbeat_seconds, self._timing.heartbeat_seconds)
        subscription = controller.watch_state()
        published: MowerState | None = None
        previous_activity: MowerActivity | None = None
        published_at = loop.time()
        rechecked_at = loop.time()
        try:
            while True:
                heartbeat = (
                    drive_heartbeat
                    if controller.drive_session_active
                    else self._timing.heartbeat_seconds
                )
                elapsed = loop.time() - published_at
                # Wake at least every drive heartbeat to notice a drive session that opened.
                timeout = max(0.0, min(heartbeat - elapsed, drive_heartbeat))
                try:
                    state = await asyncio.wait_for(anext(subscription), timeout)
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    if loop.time() - published_at < heartbeat:
                        continue
                    latest = controller.last_state or self.last_state
                    if latest is None or not controller.connection.is_connected:
                        # nothing to repeat, or the link is down (the watch ends the run)
                        published_at = loop.time()
                        continue
                    state = latest
                    rechecked_at = self._recheck_due(controller, rechecked_at)
                    await self._publish_path(controller)
                else:
                    if links_changed(self.last_state, state):
                        # The capabilities follow the links (manual driving needs the bridge).
                        self._recheck_snapshots_later(controller)
                        rechecked_at = loop.time()
                    self.last_state = state
                    self.last_activity_at = self._clock()
                    if previous_activity in _JOB_ACTIVITIES and state.activity in _AT_REST:
                        self._refresh_map_later(controller)
                    previous_activity = state.activity
                    rechecked_at = self._recheck_due(controller, rechecked_at)
                    await self._publish_path(controller)
                    if (
                        not is_significant_change(published, state)
                        and loop.time() - published_at < heartbeat
                    ):
                        continue
                await self._publish_state(state)
                published = state
                published_at = loop.time()
        finally:
            await subscription.aclose()

    def _refresh_map_later(self, controller: MowerController) -> None:
        """A job ended: zones may have changed in the vendor app, so read the map again."""
        self._snapshots_later(controller, refresh_map=True)

    def _recheck_due(self, controller: MowerController, rechecked_at: float) -> float:
        """Start a snapshot recheck when the interval has passed; return the new mark.

        Called on state changes and on heartbeats: a resting mower sends no state changes,
        but its model, limits or dock may have arrived meanwhile.
        """
        now = asyncio.get_running_loop().time()
        if now - rechecked_at < self._timing.snapshot_recheck_seconds:
            return rechecked_at
        self._recheck_snapshots_later(controller)
        return now

    def _recheck_snapshots_later(self, controller: MowerController) -> None:
        """Compare capabilities and the cached map again; publish only what changed.

        Device information (model, limits), the dock and the RTK reference arrive after the
        connect-time publish, so a single publish at connect would miss them.
        """
        self._snapshots_later(controller, refresh_map=False)

    def _snapshots_later(self, controller: MowerController, *, refresh_map: bool) -> None:
        if not refresh_map and any(not t.done() for t in self._snapshot_tasks):
            # A recheck runs already: run once more when it is done, so a change that arrived
            # meanwhile (a link flipped during the publish) is not lost until the next interval.
            self._recheck_again = controller
            return
        task = asyncio.create_task(self._publish_snapshots(controller, refresh_map=refresh_map))
        self._snapshot_tasks.add(task)
        task.add_done_callback(self._snapshot_done)

    def _snapshot_done(self, task: "asyncio.Task[None]") -> None:
        self._snapshot_tasks.discard(task)
        again, self._recheck_again = self._recheck_again, None
        if again is not None and self._controller is again:
            self._snapshots_later(again, refresh_map=False)

    async def _publish_snapshots(self, controller: MowerController, *, refresh_map: bool) -> None:
        """Publish capabilities and map when they differ from what was published before.

        A failure is logged and never ends the connection: the snapshots serve clients, the
        mower works without them.
        """
        now = self._clock()
        try:
            capabilities = await controller.capabilities(refresh=True)
            print_caps = fingerprint(capabilities)
            if print_caps != self._published_capabilities:
                snapshot = MowerCapabilitiesSnapshot(
                    mower_id=self.mower_id,
                    account_id=self.registration.account_id,
                    observed_at=now,
                    capabilities=capabilities,
                )
                # The msg id covers observed_at: a value that flips back within the stream's
                # duplicate window (manual driving with the bridge) must not be dropped.
                await self._publisher.capabilities_snapshot(snapshot, fingerprint(snapshot))
                self._published_capabilities = print_caps
        except Exception as exc:
            _log.warning(
                "capabilities snapshot failed", mower_id=str(self.mower_id), error=repr(exc)
            )
        try:
            # rebuild: a dock or reference learned after loading reaches the fingerprint
            mower_map = await controller.mower_map(refresh=refresh_map, rebuild=True)
            print_map = fingerprint(mower_map)
            if print_map != self._published_map:
                snapshot_map = MowerMapSnapshot(
                    mower_id=self.mower_id,
                    account_id=self.registration.account_id,
                    observed_at=now,
                    map=mower_map,
                )
                await self._publisher.map_snapshot(snapshot_map, fingerprint(snapshot_map))
                self._published_map = print_map
        except Exception as exc:
            _log.warning("map snapshot failed", mower_id=str(self.mower_id), error=repr(exc))

    async def _publish_path(self, controller: MowerController) -> None:
        """Publish the mow path of the running job when it changed (see
        :func:`~device_gateway.domain.rules.path_publish_due`); a cleared path publishes
        ``path = None``. Reading it costs nothing (the adapter holds it), so it is checked on
        every state. A failure is logged and never ends the connection."""
        async with self._path_lock:
            try:
                path = controller.current_path()
            except Exception as exc:
                _log.warning("mow path read failed", mower_id=str(self.mower_id), error=repr(exc))
                return
            now = asyncio.get_running_loop().time()
            if not path_publish_due(
                self._published_path,
                path,
                since_last_seconds=now - self._path_published_at,
                progress_interval_seconds=self._timing.path_progress_seconds,
            ):
                return
            snapshot = MowerPathSnapshot(
                mower_id=self.mower_id,
                account_id=self.registration.account_id,
                observed_at=self._clock(),
                path=path,
            )
            try:
                await self._publisher.path_snapshot(snapshot, fingerprint(snapshot))
            except Exception as exc:
                _log.warning("path snapshot failed", mower_id=str(self.mower_id), error=repr(exc))
                return
            progress = path.progress.model_dump_json() if path and path.progress else ""
            self._published_path = (path_geometry_key(path), progress)
            self._path_published_at = now
            if path is None:
                _log.debug("mow path cleared", mower_id=str(self.mower_id))

    # --- mow path preview ---------------------------------------------------------------------

    async def preview_path(
        self, area_ids: Sequence[str], pattern: Mapping[str, Any] | None
    ) -> MowPath:
        """Let the mower plan the route of a start without starting it.

        Raises :class:`~mowdeck_control.NotConnected` while the mower is not connected,
        :class:`PathPreviewUnsupported` when the adapter cannot, and the controller's
        ``CommandRejected`` and ``PathUnavailable``.
        """
        controller = self._controller
        if controller is None:
            raise NotConnected("the mower is not connected")
        if not controller.can_preview_path:
            raise PathPreviewUnsupported("this connection cannot plan a route without starting")
        return await controller.preview_path(area_ids, pattern)

    async def _publish_state(self, state: MowerState) -> None:
        """Publish ``state`` as seen over a live link: ``heartbeat_at`` now, ``connected``."""
        now = self._clock()
        self.heartbeat_at = now
        await self._send_state(state.model_copy(update={"heartbeat_at": now, "connected": True}))

    async def _publish_disconnected(self, state: MowerState | None) -> None:
        """The link ended: publish the last values once more with ``connected = false`` and the
        last ``heartbeat_at``, so clients show the mower offline without waiting for the
        heartbeats to go missing. Nothing when the mower never reported."""
        if state is None:
            return
        await self._send_state(
            state.model_copy(update={"heartbeat_at": self.heartbeat_at, "connected": False})
        )

    async def _send_state(self, state: MowerState) -> None:
        try:
            await self._publisher.telemetry(state)
        except Exception as exc:
            _log.warning("telemetry publish failed", mower_id=str(self.mower_id), error=repr(exc))

    async def _connection_event(self, event_type: str, reason: str | None) -> None:
        payload = MowerConnectionEvent(
            mower_id=self.mower_id,
            account_id=self.registration.account_id,
            connection_kind=self.registration.connection_kind,
            at=self._clock(),
            reason=reason,
        )
        try:
            await self._publisher.connection_event(event_type, payload)
        except Exception as exc:
            _log.warning(
                "connection event publish failed",
                mower_id=str(self.mower_id),
                event_type=event_type,
                error=repr(exc),
            )

    # --- camera -------------------------------------------------------------------------------

    async def camera_session(self) -> CameraSession | None:
        """A live camera session from the connection (ADR 0032); None without a camera.

        Raises :class:`~mowdeck_control.NotConnected` while the mower is not connected.
        """
        controller = self._controller
        if controller is None:
            raise NotConnected("mower is not connected")
        return await camera_session_of(controller.connection)

    async def release_camera(self, channel: str | None = None) -> bool:
        """Tell the mower to stop publishing its camera stream (ADR 0045); False when the
        connection cannot release (nothing was sent).

        Raises :class:`~mowdeck_control.NotConnected` while the mower is not connected or the
        stop could not be sent.
        """
        controller = self._controller
        if controller is None:
            raise NotConnected("mower is not connected")
        return await release_camera_session_of(controller.connection, channel)

    # --- commands -----------------------------------------------------------------------------

    def submit(self, envelope: CommandEnvelope) -> asyncio.Task[CommandResult]:
        """Execute ``envelope`` in its own task; its result is published when done."""
        task = asyncio.create_task(self._execute(envelope), name=f"cmd-{envelope.command_id}")
        self._commands.add(task)
        task.add_done_callback(self._commands.discard)
        return task

    async def _execute(self, envelope: CommandEnvelope) -> CommandResult:
        controller = self._controller
        if controller is None:
            result = CommandResult(
                command_id=envelope.command_id,
                mower_id=envelope.mower_id,
                status=CommandStatus.FAILED,
                reason="mower is not connected",
                at=self._clock(),
                correlation_id=envelope.correlation_id,
            )
        else:
            result = await controller.handle(envelope)
        self.last_activity_at = self._clock()
        await self._publish_result(result)
        if (
            controller is not None
            and isinstance(envelope.command, SpotMow)
            and result.status is CommandStatus.ACCEPTED
        ):
            task = asyncio.create_task(
                self._spot_final(controller, envelope), name=f"spot-{envelope.command_id}"
            )
            self._spot_followups.add(task)
            task.add_done_callback(self._spot_followups.discard)
        if (
            controller is not None
            and isinstance(envelope.command, DriveCalibrate)
            and result.status is CommandStatus.ACCEPTED
        ):
            task = asyncio.create_task(
                self._calibration_final(controller, envelope),
                name=f"calibration-{envelope.command_id}",
            )
            self._spot_followups.add(task)
            task.add_done_callback(self._spot_followups.discard)
        return result

    async def _calibration_final(
        self, controller: MowerController, envelope: CommandEnvelope
    ) -> None:
        """Wait for the end of the calibration of ``envelope`` and publish its final result:
        ``executed`` with the fit, ``failed`` with the reason."""
        run = await controller.calibration_result(envelope.command_id)
        if run is None:
            status, reason = CommandStatus.FAILED, "calibration ended without a result"
        else:
            status = CommandStatus.EXECUTED if run.state == "done" else CommandStatus.FAILED
            reason = run.summary()
        _log.info(
            "drive calibration ended",
            mower_id=str(self.mower_id),
            command_id=str(envelope.command_id),
            state=run.state if run else None,
            reason=run.reason if run else None,
        )
        await self._publish_result(
            CommandResult(
                command_id=envelope.command_id,
                mower_id=envelope.mower_id,
                status=status,
                reason=reason[:500],
                at=self._clock(),
                correlation_id=envelope.correlation_id,
            )
        )

    async def _spot_final(self, controller: MowerController, envelope: CommandEnvelope) -> None:
        """Wait for the end of the spot run of ``envelope`` and publish its final result."""
        report = await controller.spot_run_result(envelope.command_id)
        if report is None:
            status, reason = CommandStatus.FAILED, "spot run ended without a report"
        else:
            status, reason = report.command_status, report.summary()
        _log.info(
            "spot run ended",
            mower_id=str(self.mower_id),
            command_id=str(envelope.command_id),
            state=report.state if report else None,
            reason=report.reason if report else None,
        )
        # The path of the run is gone now: publish the cleared one without waiting for a state.
        await self._publish_path(controller)
        await self._publish_result(
            CommandResult(
                command_id=envelope.command_id,
                mower_id=envelope.mower_id,
                status=status,
                reason=reason,
                at=self._clock(),
                correlation_id=envelope.correlation_id,
            )
        )

    async def _publish_result(self, result: CommandResult) -> None:
        try:
            await self._publisher.command_result(result)
        except Exception as exc:
            _log.error(
                "command result publish failed",
                command_id=str(result.command_id),
                status=str(result.status),
                error=repr(exc),
            )
