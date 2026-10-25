"""Manual driving over the Mammotion cloud (ADR 0049): a :class:`~mowdeck_control.DriveSession`
on PyMammotion's remote-drive session (:class:`~mowdeck_control_mammotion.port.RemoteDriveLink`).

The vendor's "FPV control" works with a control token: :meth:`CloudDriveSession.begin`
requests it (``start``), confirms the vendor's safety notice at once (``confirm``: starting a
MowDeck drive session is the user's explicit action, ADR 0026) and then, like the Bluetooth
session, sends one ``operate_on_device`` with the blades off and ``max_run_speed``. Frames are
the same wire units as Bluetooth (:func:`~mowdeck_control_mammotion.mapping.movement_wire`).

Differences to the Bluetooth session and how this module keeps MowDeck's guarantees:

* **No reverse.** The vendor clamps a negative linear speed to 0 over the cloud. The connection
  reports ``drive_reverse`` false, the controller rejects backwards frames, and a backwards
  frame that still reaches :meth:`CloudDriveSession.update` is refused here as well (never cut
  to zero silently).
* **The link keeps moving the mower.** After every acknowledgement PyMammotion re-sends the last
  non-zero speed every 150 ms, and the server ends an idle session only after its
  ``timeout_exit`` (180 s). So a zero frame must be sent explicitly whenever MowDeck's frames
  stop: the controller's dead-man timer calls :meth:`CloudDriveSession.stop`, and a watchdog
  here (:meth:`expect_next_frame_within`, :class:`~mowdeck_control.FrameDeadlineCapable`) zeroes
  the wheels and switches the blades off on its own when a frame's ``ttl`` passed without a newer
  one. :meth:`stop`, :meth:`close` and every end the vendor reports zero the wheels;
  :meth:`close` also releases the token (``stop`` of the vendor session: a forced zero frame and
  the exit notice).
* **Frames never block the controller.** A cloud send takes 200 to 500 ms; :meth:`update` only
  stores the newest wire values and a sender task hands them to the vendor session (which
  itself keeps one frame in flight and sends the newest pending one after the ack).
* **Blades** work as over Bluetooth: ``operate_on_device`` (``DrvMowCtrlByHand``, a separate
  driver message next to the remote-drive frames ``DrvSessionCtrlReq``, which carry no blade
  field) on the cloud transport, only when a frame asks for them and the connection allows
  them; a failed switch-on refuses the frame (:class:`~mowdeck_control.CommandRejected`), a
  switch-off is tried at every stop and end and never raises.
* **Vendor ends.** A refused token, another driver, a slow network, an expired token, the mower
  leaving its map and the idle exit end the session on the vendor's side; the next
  :meth:`update` raises with the reason (:func:`end_reason`): a lost or too slow link as
  :class:`~mowdeck_control.NotConnected` (a spot run ends ``link_lost``), the rest as
  :class:`~mowdeck_control.CommandRejected`. A token refused with envelope code ``50803`` means
  the linked Mammotion account is not the mower's owner (:data:`OWNER_ACCOUNT_REQUIRED`).
* **Fence warning.** When the mower reports being ``APPROACH_FENCE_DISTANCE_M`` (5 m) or more
  beyond its map, the vendor stops it and drops input until the warning is acknowledged. MowDeck
  has no in-session acknowledgement: the session ends with :data:`FENCE_WARNING` (shown as the
  end reason of the drive session). Starting a new session within
  :data:`FENCE_ACK_WINDOW_S` counts as the acknowledgement: that session acknowledges the
  warning at once, so the mower can be driven back.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Final

from mowdeck_control import CommandRejected, NotConnected, SensorStop

from mowdeck_control_mammotion.mapping import movement_wire
from mowdeck_control_mammotion.port import (
    MammotionPort,
    NoTransportAvailableError,
    PortClosedError,
    Priority,
    RemoteDriveInfo,
    RemoteDriveLink,
)

_log = logging.getLogger("mowdeck_control_mammotion.cloud_drive")

NO_PERMISSION_CODE: Final = "50803"
"""Envelope code of a token request by an account that does not own the mower ("No permission
to operate the device"); PyMammotion reports it as ``token_unavailable`` with this detail."""
OWNER_ACCOUNT_REQUIRED: Final = (
    "cloud driving needs the mower's owner account: the linked Mammotion account only has the "
    "mower shared (code 50803); link the account that owns the mower or use the Bluetooth bridge"
)
FENCE_WARNING: Final = (
    "the mower is 5 m or more beyond its mapped area (vendor fence warning) and stopped; start "
    "driving again within 5 minutes to acknowledge it and drive the mower back"
)
NO_REVERSE: Final = "the Mammotion cloud cannot drive backwards"
CLOUD_UNAVAILABLE: Final = "no usable Mammotion cloud connection for driving"
BLADES_NOT_SWITCHED: Final = "the blades could not be switched on over the Mammotion cloud"
FENCE_ACK_WINDOW_S: Final = 300.0
"""A session started this long after a fence warning acknowledges the next one at once."""
DEFAULT_FRAME_TTL_S: Final = 1.0
"""Watchdog deadline of a frame without :meth:`CloudDriveSession.expect_next_frame_within`
(the default ``ttl_ms`` of a ``ManualDrive`` frame)."""
WATCH_TICK_S: Final = 0.05
"""Granularity of the watchdog."""
SEND_TIMEOUT_S: Final = 5.0
"""Longest wait for one hand-over to the vendor session (it bounds its own sends at 1 s)."""

_EVENT_REASONS: Final[dict[str, str]] = {
    "token_unavailable": "the Mammotion cloud gave no drive token",
    "occupied_by_other": "someone else is driving the mower (the Mammotion app or another session)",
    "network_poor": "the cloud connection to the mower is too slow for driving",
    "token_expired": "the cloud drive token expired",
    "out_of_fence": "the mower left its mapped area; the cloud drive session ended",
    "ble_preempt": "the Mammotion app took over the mower over Bluetooth",
    "no_loc_mileage_exhausted": "the mower drove too far without a position fix",
    "env_not_ready": "the mower is not ready for cloud driving",
    "send_failed": "a drive frame could not be sent over the Mammotion cloud",
    "idle_timeout": "the cloud drive session ended after a long time without input",
    "safety_notice_timeout": "the cloud drive session was not confirmed in time",
    "video_unavailable": "the cloud drive session needs the camera",
    "approach_fence": FENCE_WARNING,
}
_LINK_LOST_KINDS: Final = frozenset({"network_poor", "token_expired", "send_failed", "ble_preempt"})
"""Vendor ends that mean the drive link is gone (``NotConnected``), not a refusal."""
_RUNNING_KINDS: Final = frozenset({"approach_fence", "latency_high"})
"""Events after which the vendor session keeps running."""


def end_reason(event: RemoteDriveInfo | None) -> str:
    """The user-facing reason for a vendor end or refusal (no token, no account names)."""
    if event is None:
        return "the Mammotion cloud refused the drive session"
    if event.kind == "token_unavailable" and event.detail == NO_PERMISSION_CODE:
        return OWNER_ACCOUNT_REQUIRED
    reason = _EVENT_REASONS.get(event.kind, f"the cloud drive session ended ({event.kind})")
    if event.kind in ("token_unavailable", "env_not_ready") and event.detail:
        reason += f" (code {event.detail[:16]})"
    return reason


def is_link_loss(event: RemoteDriveInfo) -> bool:
    """True when the vendor end means the drive link is gone."""
    return event.kind in _LINK_LOST_KINDS


class CloudDriveSession:
    """One open joystick session over the Mammotion cloud (see the module docstring)."""

    def __init__(
        self,
        port: MammotionPort,
        link: RemoteDriveLink,
        *,
        blades_allowed: bool,
        blade_height_mm: int,
        blades_on_height_mm: int | None = None,
        sensor_stop: Callable[[], str | None],
        max_run_speed_mps: float,
        fence_acknowledged: bool = False,
        on_end: Callable[[RemoteDriveInfo], None] | None = None,
        frame_ttl_s: float = DEFAULT_FRAME_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._port = port
        self._link = link
        self._blades_allowed = blades_allowed
        self._blade_height_mm = blade_height_mm
        self._blades_on_height_mm = (
            blade_height_mm if blades_on_height_mm is None else blades_on_height_mm
        )
        """``cut_knife_height`` when switching the blades on (0 for the Yuka family,
        :func:`~mowdeck_control_mammotion.mapping.manual_blades_on_height`)."""
        self._sensor_stop = sensor_stop
        self._max_run_speed = max_run_speed_mps
        self._fence_acknowledged = fence_acknowledged
        self._on_end = on_end
        self._default_ttl_s = frame_ttl_s
        self._clock = clock
        self._open = False
        self._closing = False
        self._blades_on = False
        self._ended: RemoteDriveInfo | None = None
        """The vendor event that ended the session (None while it runs)."""
        self._refusal: RemoteDriveInfo | None = None
        self._target: tuple[int, int] = (0, 0)
        self._sent: tuple[int, int] | None = None
        self._deadline: float | None = None
        self._wake = asyncio.Event()
        self._sender: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self.watchdog_stops = 0
        """How often the watchdog zeroed the wheels (diagnostics and tests)."""

    # --- DriveSession -----------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """True until :meth:`close`, also after a vendor end: the next frame then raises with
        the reason (the controller ends the session and shows it) instead of a new session
        opening silently."""
        return self._open

    @property
    def driving(self) -> bool:
        """Open and not ended by the vendor (the refresh loop's ``drive`` mode)."""
        return self._open and self._ended is None

    @property
    def blades_on(self) -> bool:
        """Whether the last command switched the blades on."""
        return self._blades_on

    @property
    def max_run_speed_mps(self) -> float:
        """The ``max_run_speed`` this session sends with every ``operate_on_device``."""
        return self._max_run_speed

    @property
    def ended(self) -> RemoteDriveInfo | None:
        """The vendor event that ended the session, None while it runs."""
        return self._ended

    async def begin(self) -> None:
        """Request the token, confirm the safety notice and send the blades-off
        ``operate_on_device`` with ``max_run_speed``. Raises
        :class:`~mowdeck_control.CommandRejected` when the vendor refuses (with the reason,
        :data:`OWNER_ACCOUNT_REQUIRED` for a shared account) and
        :class:`~mowdeck_control.NotConnected` without a cloud transport."""
        self._unsubscribe = self._link.subscribe(self._on_event)
        try:
            if self._link.phase != "idle":
                await self._link.stop()  # a leftover of a session that did not end cleanly
            granted = await self._link.start()
            if not granted:
                raise CommandRejected(end_reason(self._refusal))
            await self._link.confirm()
        except (NoTransportAvailableError, PortClosedError) as exc:
            await self._abandon()
            raise NotConnected(CLOUD_UNAVAILABLE) from exc
        except CommandRejected:
            await self._abandon()
            raise
        except Exception as exc:
            _log.warning("cloud drive start of %s failed: %s", self._name(), type(exc).__name__)
            await self._abandon()
            msg = f"the Mammotion cloud refused the drive session ({type(exc).__name__})"
            raise CommandRejected(msg) from exc
        self._open = True
        await self._send_blades_off(Priority.USER)
        self._sender = asyncio.create_task(self._send_loop(), name=f"cloud-drive-{self._name()}")
        self._watchdog = asyncio.create_task(self._watch(), name=f"cloud-watchdog-{self._name()}")
        _log.info(
            "cloud drive session of %s open (max_run_speed %.2f m/s)",
            self._name(),
            self._max_run_speed,
        )

    async def update(self, linear: float, angular: float, blade_on: bool) -> None:
        if self._ended is not None:
            event = self._ended
            if is_link_loss(event):
                raise NotConnected(end_reason(event))
            raise CommandRejected(end_reason(event))
        if not self._open:
            raise CommandRejected("drive session is closed")
        if not self._port.link_up:
            raise NotConnected(CLOUD_UNAVAILABLE)
        reason = self._sensor_stop()
        if reason is not None:
            await self.stop()
            raise SensorStop(reason)
        if linear < 0:
            # The controller refuses these first (capability drive_reverse is false); never cut
            # the backwards part off silently here either.
            raise CommandRejected(NO_REVERSE)
        want_blades = blade_on and self._blades_allowed
        if want_blades != self._blades_on:
            if want_blades:
                await self._switch_blades_on()
            else:
                await self._send_blades_off(Priority.USER)
        self._target = movement_wire(linear, angular)
        self._deadline = self._clock() + self._default_ttl_s
        self._wake.set()

    def expect_next_frame_within(self, ttl_s: float) -> None:
        """:class:`~mowdeck_control.FrameDeadlineCapable`: the watchdog zeroes the wheels when
        no newer frame arrives within ``ttl_s``."""
        if self._open:
            self._deadline = self._clock() + max(0.05, ttl_s)
            self._wake.set()

    async def stop(self) -> None:
        """Zero the wheels and switch the blades off now; the session stays open. Never
        raises."""
        self._target = (0, 0)
        self._deadline = None
        await self._send_zero()
        await self._send_blades_off(Priority.EMERGENCY)

    async def close(self) -> None:
        """Stop, release the token and end the tasks. Idempotent, never raises."""
        if self._closing:
            return
        self._closing = True
        was_open, self._open = self._open, False
        try:
            if was_open:
                await self.stop()
        finally:
            for task in (self._sender, self._watchdog):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            self._sender = self._watchdog = None
            try:
                await self._link.stop()  # forced zero frame and token release when active
            except Exception:
                _log.warning("cloud drive release of %s failed", self._name(), exc_info=True)
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            if unsubscribe is not None:
                with contextlib.suppress(Exception):
                    unsubscribe()
        _log.info("cloud drive session of %s closed", self._name())

    # --- internals --------------------------------------------------------------------------

    def _name(self) -> str:
        try:
            return self._port.device_name
        except Exception:
            return "mower"

    async def _abandon(self) -> None:
        with contextlib.suppress(Exception):
            await self._link.stop()
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            with contextlib.suppress(Exception):
                unsubscribe()

    async def _on_event(self, event: RemoteDriveInfo) -> None:
        if event.kind == "latency_high":
            _log.warning("cloud drive of %s: high latency (%s ms)", self._name(), event.detail)
            return
        if event.kind == "approach_fence":
            if self._fence_acknowledged:
                # The user started this session after the warning: that is the acknowledgement.
                self._link.acknowledge_fence_warning()
                _log.warning(
                    "cloud drive of %s: fence warning acknowledged by the new session",
                    self._name(),
                )
                return
            self._end_with(event)
            # The vendor already sent a zero frame and drops input; end its session too.
            with contextlib.suppress(Exception):
                await self._link.stop()
            await self._send_blades_off(Priority.EMERGENCY)
            return
        if not self._open:
            self._refusal = event  # a refusal during begin(): its reason
            return
        if event.kind in _RUNNING_KINDS:
            return
        self._end_with(event)
        # The vendor stopped the mower and released the token; switch the blades off as well.
        await self._send_blades_off(Priority.EMERGENCY)

    def _end_with(self, event: RemoteDriveInfo) -> None:
        if self._ended is not None:
            return
        self._ended = event
        self._target = (0, 0)
        self._deadline = None
        _log.warning("cloud drive of %s ended by the vendor: %s", self._name(), end_reason(event))
        if self._on_end is not None:
            try:
                self._on_end(event)
            except Exception:
                _log.exception("cloud drive end callback failed")

    async def _send_loop(self) -> None:
        """Hand the newest target to the vendor session whenever it changed."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            target = self._target
            if target == self._sent or self._ended is not None:
                continue
            try:
                async with asyncio.timeout(SEND_TIMEOUT_S):
                    await self._link.drive(*target)
                self._sent = target
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning("cloud drive frame of %s failed: %s", self._name(), type(exc).__name__)
                self._sent = None

    async def _watch(self) -> None:
        """Zero the wheels (and switch the blades off) when the newest frame's deadline passed."""
        while True:
            deadline = self._deadline
            now = self._clock()
            if deadline is None or now < deadline:
                wait = WATCH_TICK_S if deadline is None else min(deadline - now, WATCH_TICK_S)
                await asyncio.sleep(wait)
                continue
            self._deadline = None
            if self._target != (0, 0) or self._sent != (0, 0) or self._blades_on:
                self.watchdog_stops += 1
                _log.warning(
                    "cloud drive of %s: no frame within its ttl, zeroing the wheels", self._name()
                )
                await self.stop()

    async def _send_zero(self) -> None:
        """A zero frame straight to the vendor session (never raises)."""
        try:
            async with asyncio.timeout(SEND_TIMEOUT_S):
                await self._link.drive(0, 0)
            self._sent = (0, 0)
        except Exception:
            _log.warning("cloud drive zero frame of %s failed", self._name(), exc_info=True)
            self._sent = None

    async def _switch_blades_on(self) -> None:
        try:
            await self._operate(on=True, priority=Priority.USER)
        except (NoTransportAvailableError, PortClosedError) as exc:
            raise NotConnected(CLOUD_UNAVAILABLE) from exc
        except Exception as exc:
            _log.warning("blades on over the cloud failed: %s", type(exc).__name__)
            raise CommandRejected(BLADES_NOT_SWITCHED) from exc

    async def _send_blades_off(self, priority: Priority) -> None:
        """``operate_on_device`` with the blades off (never raises)."""
        try:
            await self._operate(on=False, priority=priority)
        except Exception:  # the link may be gone; the vendor stops the blades with the session
            _log.warning("cloud drive blades off of %s failed", self._name(), exc_info=True)

    async def _operate(self, *, on: bool, priority: Priority) -> None:
        await self._port.send(
            "operate_on_device",
            priority=priority,
            main_ctrl=1 if on else 0,
            cut_knife_ctrl=1 if on else 0,
            cut_knife_height=self._blades_on_height_mm if on else self._blade_height_mm,
            max_run_speed=self._max_run_speed,
        )
        self._blades_on = on
