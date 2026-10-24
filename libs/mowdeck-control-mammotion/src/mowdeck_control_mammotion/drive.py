"""Manual driving over BLE: a :class:`~mowdeck_control.DriveSession` on ``send_movement``.

Each :meth:`BleDriveSession.update` sends one movement frame at the controller's cadence (the
controller owns the dead-man timer; there is no timer here). Blades are switched with
``operate_on_device`` and only when a frame asks for them and the connection allows them;
otherwise they are kept off. :meth:`stop` sends a zero movement and blades off; both
:meth:`stop` and :meth:`close` are idempotent and never raise after a link loss.

:meth:`BleDriveSession.begin` opens the session with one ``operate_on_device`` (blades off)
carrying ``max_run_speed`` (ADR 0042): the vendor app's joystick sends this message with the
manual-driving speed, while ``send_movement`` alone carries no speed limit. Before, a session
with the blades off never sent it, so the mower kept whatever ``max_run_speed`` it had last
received (the emergency stop sends 0.0). The value is :func:`manual_run_speed`; every later
``operate_on_device`` of the session (blades, stop) repeats it.

With a dual link (ADR 0038) the session gets ``link_up``: a frame while the Bluetooth bridge is
down closes the session and raises :class:`~mowdeck_control.NotConnected` instead of letting
PyMammotion route the movement over the cloud.
"""

import logging
from collections.abc import Callable

from mowdeck_control import CommandRejected, NotConnected, SensorStop

from mowdeck_control_mammotion.mapping import movement_wire
from mowdeck_control_mammotion.port import (
    MammotionPort,
    NoTransportAvailableError,
    PortClosedError,
    Priority,
)

_log = logging.getLogger("mowdeck_control_mammotion.drive")

MANUAL_RUN_SPEED_CAP_MPS = 1.0
"""Highest ``max_run_speed`` a drive session sends (PyMammotion's blade helper in
``mower_api.py`` sends 1.2; MowDeck stays below it for manual driving)."""


def manual_run_speed(option_mps: float | None, setting_mps: float | None) -> float:
    """``max_run_speed`` of a drive session: the link option ``manual_drive_speed_mps`` when
    set, else the mower's own speed setting for manual driving (``mower_state.travel_speed``,
    the answer to ``get_speed``), else the cap; never above :data:`MANUAL_RUN_SPEED_CAP_MPS`."""
    for value in (option_mps, setting_mps):
        if value is not None and value > 0:
            return round(min(float(value), MANUAL_RUN_SPEED_CAP_MPS), 2)
    return MANUAL_RUN_SPEED_CAP_MPS


class BleDriveSession:
    """One open joystick session over the BLE link."""

    def __init__(
        self,
        port: MammotionPort,
        *,
        blades_allowed: bool,
        blade_height_mm: int,
        blades_on_height_mm: int | None = None,
        sensor_stop: Callable[[], str | None],
        link_up: Callable[[], bool] | None = None,
        max_run_speed_mps: float = MANUAL_RUN_SPEED_CAP_MPS,
    ) -> None:
        self._port = port
        self._max_run_speed = max_run_speed_mps
        self._blades_allowed = blades_allowed
        self._blade_height_mm = blade_height_mm
        self._blades_on_height_mm = (
            blade_height_mm if blades_on_height_mm is None else blades_on_height_mm
        )
        """``cut_knife_height`` when switching the blades on (0 for the Yuka family,
        :func:`~mowdeck_control_mammotion.mapping.manual_blades_on_height`)."""
        self._sensor_stop = sensor_stop
        self._link_up = link_up
        self._open = True
        self._blades_on = False

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def blades_on(self) -> bool:
        """Whether the last command switched the blades on."""
        return self._blades_on

    @property
    def max_run_speed_mps(self) -> float:
        """The ``max_run_speed`` this session sends with every ``operate_on_device``."""
        return self._max_run_speed

    async def begin(self) -> None:
        """Open the session on the mower: ``operate_on_device`` with the blades off and
        ``max_run_speed``, so the stick frames run at the manual-driving speed instead of a
        value left over from an earlier command. A transport error closes the session and
        raises :class:`~mowdeck_control.NotConnected`."""
        try:
            await self._blades(on=False)
        except (NoTransportAvailableError, PortClosedError) as exc:
            self._open = False
            raise NotConnected("BLE link to the mower lost") from exc

    async def update(self, linear: float, angular: float, blade_on: bool) -> None:
        if not self._open:
            raise CommandRejected("drive session is closed")
        if self._link_up is not None and not self._link_up():
            # Dual link: the frame must never fall back to the cloud transport.
            self._open = False
            raise NotConnected("the Bluetooth bridge is not connected")
        reason = self._sensor_stop()
        if reason is not None:
            await self.stop()
            self._open = False
            raise SensorStop(reason)
        want_blades = blade_on and self._blades_allowed
        try:
            if want_blades != self._blades_on:
                await self._blades(on=want_blades)
            linear_speed, angular_speed = movement_wire(linear, angular)
            await self._port.send(
                "send_movement",
                priority=Priority.USER,
                linear_speed=linear_speed,
                angular_speed=angular_speed,
            )
        except (NoTransportAvailableError, PortClosedError) as exc:
            raise NotConnected("BLE link to the mower lost") from exc

    async def stop(self) -> None:
        for name, kwargs in (
            ("send_movement", {"linear_speed": 0, "angular_speed": 0}),
            (
                "operate_on_device",
                {
                    "main_ctrl": 0,
                    "cut_knife_ctrl": 0,
                    "cut_knife_height": self._blade_height_mm,
                    "max_run_speed": self._max_run_speed,
                },
            ),
        ):
            try:
                await self._port.send(name, priority=Priority.EMERGENCY, **kwargs)
            except Exception:  # stop must never raise, the link may be gone
                _log.warning("drive stop: %s failed", name, exc_info=True)
        self._blades_on = False

    async def close(self) -> None:
        if self._open:
            await self.stop()
            self._open = False

    async def _blades(self, *, on: bool) -> None:
        await self._port.send(
            "operate_on_device",
            priority=Priority.USER,
            main_ctrl=1 if on else 0,
            cut_knife_ctrl=1 if on else 0,
            cut_knife_height=self._blades_on_height_mm if on else self._blade_height_mm,
            max_run_speed=self._max_run_speed,
        )
        self._blades_on = on
