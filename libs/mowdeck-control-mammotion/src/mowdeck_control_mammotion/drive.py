"""Manual driving over BLE: a :class:`~mowdeck_control.DriveSession` on ``send_movement``.

Each :meth:`BleDriveSession.update` sends one movement frame at the controller's cadence (the
controller owns the dead-man timer; there is no timer here). Blades are switched with
``operate_on_device`` and only when a frame asks for them and the connection allows them;
otherwise they are kept off. :meth:`stop` sends a zero movement and blades off; both
:meth:`stop` and :meth:`close` are idempotent and never raise after a link loss.
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

MAX_RUN_SPEED_MPS = 1.2
"""``max_run_speed`` of ``operate_on_device`` as Mammotion-HA sends it."""


class BleDriveSession:
    """One open joystick session over the BLE link."""

    def __init__(
        self,
        port: MammotionPort,
        *,
        blades_allowed: bool,
        blade_height_mm: int,
        sensor_stop: Callable[[], str | None],
    ) -> None:
        self._port = port
        self._blades_allowed = blades_allowed
        self._blade_height_mm = blade_height_mm
        self._sensor_stop = sensor_stop
        self._open = True
        self._blades_on = False

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def blades_on(self) -> bool:
        """Whether the last command switched the blades on."""
        return self._blades_on

    async def update(self, linear: float, angular: float, blade_on: bool) -> None:
        if not self._open:
            raise CommandRejected("drive session is closed")
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
                    "max_run_speed": MAX_RUN_SPEED_MPS,
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
            cut_knife_height=self._blade_height_mm,
            max_run_speed=MAX_RUN_SPEED_MPS,
        )
        self._blades_on = on
