"""One mower held by the gateway: its connection, controller, telemetry and commands.

A :class:`MowerSession` supervises the link: it creates the connection through the
:class:`~device_gateway.application.ports.ConnectionProvider`, wraps it in a
:class:`~mowdeck_control.MowerController`, publishes telemetry and connection events, and
reconnects with backoff when the link drops. Commands run as one task each; the controller
serializes ordinary commands of this mower and lets an emergency stop overtake them, and no
mower ever waits for another one.
"""

import asyncio
import contextlib
import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from mowdeck_contracts.v1 import (
    MOWER_CONNECTED,
    MOWER_DISCONNECTED,
    CameraSession,
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    MowerActivity,
    MowerCapabilitiesSnapshot,
    MowerConnectionEvent,
    MowerMapSnapshot,
    MowerRegistration,
    MowerState,
    utc_now,
)
from mowdeck_control import (
    MowerConnection,
    MowerController,
    NotConnected,
    Photo,
    PhotoCapture,
    camera_session_of,
)
from mowdeck_service_kit import get_logger
from pydantic import BaseModel

from device_gateway.application.ports import (
    CameraSnapshotter,
    Clock,
    ConnectionProvider,
    CredentialSource,
    GatewayPublisher,
    NoCredentials,
)
from device_gateway.domain.rules import ConnectionStatus, backoff, is_significant_change

_log = get_logger(__name__)

_JOB_ACTIVITIES = frozenset({MowerActivity.MOWING, MowerActivity.PAUSED, MowerActivity.RETURNING})
_AT_REST = frozenset({MowerActivity.DOCKED, MowerActivity.CHARGING, MowerActivity.IDLE})


def fingerprint(model: BaseModel) -> str:
    """A short, stable hash of a model's JSON (change detection and msg id)."""
    return hashlib.sha256(model.model_dump_json().encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class SessionTiming:
    """Timing of one session (from the settings)."""

    heartbeat_seconds: float = 10.0
    drive_heartbeat_seconds: float = 1.0
    """Heartbeat while a drive session is open: automated driving (AI assist) aborts when no
    state arrives for a few seconds, also when the mower does not move (stuck)."""
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    connection_check_seconds: float = 2.0
    snapshot_recheck_seconds: float = 30.0
    """Capabilities and map are compared again (and republished when changed) this often
    while states arrive: a mower learns its model, limits or dock only after connecting."""


class ConnectionLost(Exception):  # noqa: N818 - reads as an event
    """The link of an established connection dropped."""


class PhotoUnsupported(Exception):  # noqa: N818 - reads as a state
    """The adapter of the mower cannot take photos."""


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
        snapshotter: CameraSnapshotter | None = None,
        snapshot_timeout_seconds: float = 20.0,
    ) -> None:
        self.registration = registration
        self._snapshotter = snapshotter
        self._snapshot_timeout_seconds = snapshot_timeout_seconds
        self._photo_lock = asyncio.Lock()
        self._credentials: CredentialSource = credentials or NoCredentials()
        self.status = ConnectionStatus.CONNECTING
        self.connected_since: datetime | None = None
        self.last_activity_at: datetime | None = None
        self.last_state: MowerState | None = None
        self._provider = provider
        self._publisher = publisher
        self._timing = timing
        self._clock = clock
        self._controller: MowerController | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._commands: set[asyncio.Task[CommandResult]] = set()
        self._snapshot_tasks: set[asyncio.Task[None]] = set()
        self._published_map: str | None = None
        self._published_capabilities: str | None = None

    @property
    def mower_id(self) -> UUID:
        """The mower this session holds."""
        return self.registration.mower_id

    @property
    def in_flight(self) -> int:
        """Number of commands not yet answered."""
        return len(self._commands)

    async def capture_photo(self) -> Photo:
        """A still image: from the adapter (:class:`~mowdeck_control.PhotoCapture`), else a
        frame of its live camera through the capture worker (ADR 0035), one at a time.

        Raises :class:`~mowdeck_control.NotConnected` while the link is down,
        :class:`PhotoUnsupported` when neither is possible (no camera, no worker configured)
        and :class:`~device_gateway.application.ports.SnapshotFailed` when the worker gave no
        frame.
        """
        controller = self._controller
        if controller is None:
            raise NotConnected("the mower is not connected")
        connection = controller.connection
        if isinstance(connection, PhotoCapture):
            return await connection.capture_photo()
        if self._snapshotter is None:
            raise PhotoUnsupported("this connection cannot take photos")
        async with self._photo_lock:
            camera = await camera_session_of(connection)
            if camera is None:
                raise PhotoUnsupported("this connection has no camera")
            uid = snapshot_camera_uid(camera)
            photo = await self._snapshotter.snapshot(
                camera, uid, timeout_seconds=self._snapshot_timeout_seconds
            )
        _log.info("camera frame captured", mower_id=str(self.mower_id), camera_uid=uid)
        return photo

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
        self.status = ConnectionStatus.CLOSED

    async def _supervise(self) -> None:
        delays = backoff(self._timing.reconnect_initial_seconds, self._timing.reconnect_max_seconds)
        while True:
            try:
                await self._connect_and_run()
            except asyncio.CancelledError:
                raise
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
        (ADR 0034): every (re)connect asks again, nothing is kept on the session."""
        registration = self.registration
        if not registration.needs_credentials:
            return registration
        secrets = await self._credentials.fetch(registration)
        kept = registration.connection_options.items()
        options = {k: v for k, v in kept if not k.startswith("credentials")}
        options.update(secrets)
        return registration.model_copy(update={"connection_options": options})

    async def _connect_and_run(self) -> None:
        connection = self._provider.create(await self._connected_registration())
        controller = MowerController(connection, mower_id=self.mower_id)
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
            await self._run_connected(controller, connection)
            reason = "connection lost"
            raise ConnectionLost
        except asyncio.CancelledError:
            reason = "gateway released the mower"
            raise
        finally:
            self._controller = None
            self.connected_since = None
            with contextlib.suppress(Exception):
                await controller.close()
            if started:
                await self._connection_event(MOWER_DISCONNECTED, reason)

    async def _run_connected(
        self, controller: MowerController, connection: MowerConnection
    ) -> None:
        """Run telemetry, the liveness check and adapter jobs; return when one of them ends."""
        tasks = [
            asyncio.create_task(self._telemetry(controller)),
            asyncio.create_task(self._watch_link(connection)),
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

    async def _watch_link(self, connection: MowerConnection) -> None:
        # The connection protocol has no "link lost" event, so the flag is polled.
        while connection.is_connected:  # noqa: ASYNC110
            await asyncio.sleep(self._timing.connection_check_seconds)

    async def _telemetry(self, controller: MowerController) -> None:
        """Publish every significant state change, and the latest state as a heartbeat (every
        ``drive_heartbeat_seconds`` while a drive session is open)."""
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
                    if latest is None:
                        published_at = loop.time()
                        continue
                    state = latest
                    rechecked_at = self._recheck_due(controller, rechecked_at)
                else:
                    self.last_state = state
                    self.last_activity_at = self._clock()
                    if previous_activity in _JOB_ACTIVITIES and state.activity in _AT_REST:
                        self._refresh_map_later(controller)
                    previous_activity = state.activity
                    rechecked_at = self._recheck_due(controller, rechecked_at)
                    if not is_significant_change(published, state):
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
            return
        task = asyncio.create_task(self._publish_snapshots(controller, refresh_map=refresh_map))
        self._snapshot_tasks.add(task)
        task.add_done_callback(self._snapshot_tasks.discard)

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
                await self._publisher.capabilities_snapshot(
                    MowerCapabilitiesSnapshot(
                        mower_id=self.mower_id,
                        account_id=self.registration.account_id,
                        observed_at=now,
                        capabilities=capabilities,
                    ),
                    print_caps,
                )
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
                await self._publisher.map_snapshot(
                    MowerMapSnapshot(
                        mower_id=self.mower_id,
                        account_id=self.registration.account_id,
                        observed_at=now,
                        map=mower_map,
                    ),
                    print_map,
                )
                self._published_map = print_map
        except Exception as exc:
            _log.warning("map snapshot failed", mower_id=str(self.mower_id), error=repr(exc))

    async def _publish_state(self, state: MowerState) -> None:
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
        try:
            await self._publisher.command_result(result)
        except Exception as exc:
            _log.error(
                "command result publish failed",
                command_id=str(envelope.command_id),
                status=str(result.status),
                error=repr(exc),
            )
        return result
