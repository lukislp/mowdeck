"""Getting stuck, being driven free, and the still images of the simulator."""

import asyncio
import io
import math
import struct
import zlib
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandStatus,
    ManualDrive,
    MowerActivity,
    MowerCommand,
    PrincipalKind,
    PrincipalRef,
    ReturnToDock,
    SetLight,
    StartMow,
)
from mowdeck_control import (
    STUCK_ERROR_CODE,
    ConnectionConfig,
    MowerController,
    NotConnected,
    PhotoCapture,
    SimulatedMower,
    SimulationConfig,
    create_simulated_connection,
    default_map,
    flat_png,
    local_angle,
    render_scene,
)
from mowdeck_control.geo import compass_heading
from mowdeck_control.testing import ManualClock

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)


def _sim(**config: Any) -> SimulatedMower:
    return SimulatedMower(config=SimulationConfig(**config), clock=ManualClock())


def test_control_stuck_config_is_validated() -> None:
    with pytest.raises(ValueError, match="stuck_after_seconds"):
        SimulationConfig(stuck_after_seconds=0)
    with pytest.raises(ValueError, match="unstuck_backward_m"):
        SimulationConfig(unstuck_backward_m=0)
    sim = create_simulated_connection(
        ConnectionConfig(uuid4(), uuid4(), {"stuck_after_seconds": 20, "time_scale": 1})
    )
    assert sim.config.stuck_after_seconds == 20


async def test_control_stuck_after_seconds_of_mowing_and_driven_free() -> None:
    sim = _sim(stuck_after_seconds=20)
    await sim.connect()
    assert (await sim.execute(StartMow(area_ids=["front"]))).status is CommandStatus.EXECUTED
    for _ in range(200):
        state = sim.tick(1)
        if state.activity is MowerActivity.ERROR:
            break
    assert state.error_code == STUCK_ERROR_CODE
    assert state.drive_recoverable is True
    assert state.raw["stuck"] is True
    assert not sim.blade_on
    stuck_at = sim.local_position
    # Other commands are refused, the mower stays where it is.
    assert (await sim.execute(ReturnToDock())).status is CommandStatus.REJECTED
    sim.tick(10)
    assert sim.local_position == stuck_at
    session = await sim.start_drive_session()
    assert sim.stuck, "opening a drive session does not clear the stuck error"
    await session.update(1.0, 0.5, blade_on=True)
    sim.tick(2)
    assert sim.local_position == stuck_at, "forward moves and turns do nothing while stuck"
    assert not sim.blade_on, "blades never run while stuck"
    await session.update(-1.0, 0.0, blade_on=False)
    sim.tick(0.4)  # 0.2 m backwards: not yet free
    assert sim.stuck
    sim.tick(0.4)
    state = await sim.get_state()
    assert state.activity is MowerActivity.IDLE
    assert state.error_code is None and not state.drive_recoverable
    assert math.dist(sim.local_position, stuck_at) >= 0.3
    await session.update(0.0, 1.0, blade_on=False)
    before = sim.theta
    sim.tick(0.5)
    assert sim.theta != before, "turning works again once free"
    await session.close()


async def test_control_stuck_happens_once_per_job_and_inject_works() -> None:
    sim = _sim(stuck_after_seconds=5)
    await sim.connect()
    sim.inject_stuck()
    state = await sim.get_state()
    assert state.activity is MowerActivity.ERROR and state.drive_recoverable
    sim.clear_error()
    assert not sim.stuck
    assert (await sim.execute(StartMow(area_ids=["front"]))).status is CommandStatus.EXECUTED
    for _ in range(50):
        if sim.tick(1).activity is MowerActivity.ERROR:
            break
    assert sim.stuck
    sim.clear_error()
    assert (await sim.get_state()).activity is MowerActivity.PAUSED
    assert (await sim.execute(StartMow(area_ids=["front"]))).status is CommandStatus.EXECUTED
    for _ in range(3):
        sim.tick(1)
    assert not sim.stuck, "a new job counts its own mowing time"


async def test_control_lift_is_never_drive_recoverable() -> None:
    sim = _sim()
    await sim.connect()
    sim.lift()
    state = await sim.get_state()
    assert state.activity is MowerActivity.ERROR
    assert state.drive_recoverable is False


async def test_control_controller_admits_frames_only_for_recoverable_errors() -> None:
    clock = ManualClock()
    sim = SimulatedMower(clock=clock)

    def envelope(command: MowerCommand) -> CommandEnvelope:
        return CommandEnvelope.create(
            mower_id=sim.mower_id,
            account_id=sim.account_id,
            issued_by=ISSUER,
            command=command,
            ttl=timedelta(seconds=5),
            now=clock(),
        )

    async with MowerController(sim, mower_id=sim.mower_id, clock=clock) as controller:
        sim.inject_stuck()
        await asyncio.sleep(0)
        frame = envelope(ManualDrive(linear=-1, angular=0, blade_on=True))
        result = await controller.handle(frame)
        assert result.status is CommandStatus.ACCEPTED
        assert controller.drive_session_active
        assert not sim.blade_on
        sim.tick(0.8)
        await asyncio.sleep(0)
        assert not sim.stuck
        # A lift during the drive is a sensor stop: the session ends and frames are refused.
        sim.lift()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not controller.drive_session_active
        refused = await controller.handle(envelope(ManualDrive(linear=-1, angular=0)))
        assert refused.status is CommandStatus.REJECTED
        assert "sensor stop" in (refused.reason or "")


async def test_control_capture_photo_renders_the_garden() -> None:
    sim = _sim()
    assert isinstance(sim, PhotoCapture)
    with pytest.raises(NotConnected):
        await sim.capture_photo()
    await sim.connect()
    photo = await sim.capture_photo()
    assert photo.media_type == "image/jpeg"
    assert photo.data[:2] == b"\xff\xd8"
    assert 1000 < len(photo.data) < 200_000
    sim.inject_stuck()
    stuck_photo = await sim.capture_photo()
    assert stuck_photo.data != photo.data, "the stuck marker is drawn"


async def test_control_night_photos_are_dark_until_the_light_is_on() -> None:
    from PIL import Image, ImageStat

    def mean(data: bytes) -> float:
        with Image.open(io.BytesIO(data)) as image:
            return float(ImageStat.Stat(image.convert("L")).mean[0])

    day = _sim()
    await day.connect()
    night = _sim(night=True)
    await night.connect()
    bright = mean((await day.capture_photo()).data)
    dark = mean((await night.capture_photo()).data)
    await night.execute(SetLight(on=True))
    lit = mean((await night.capture_photo()).data)
    assert dark < 20 < 60 < bright
    assert dark < lit <= bright


def test_control_render_scene_and_flat_png() -> None:
    mower_map = default_map()
    photo = render_scene(mower_map, (5.0, 4.0), 0.3, width=160, height=120)
    assert photo.media_type == "image/jpeg"
    png = flat_png(4, 3, (10, 20, 30))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (4, 3)
    idat_start = png.index(b"IDAT") + 4
    length = struct.unpack(">I", png[idat_start - 8 : idat_start - 4])[0]
    raw = zlib.decompress(png[idat_start : idat_start + length])
    assert raw[:4] == b"\x00\x0a\x14\x1e"


@pytest.mark.parametrize("angle", [0.0, 1.0, -2.0, 3.0])
def test_control_local_angle_inverts_compass_heading(angle: float) -> None:
    mower_map = default_map()
    heading = compass_heading(mower_map, angle)
    back = local_angle(mower_map, heading)
    assert math.isclose(math.cos(back), math.cos(angle), abs_tol=1e-9)
    assert math.isclose(math.sin(back), math.sin(angle), abs_tol=1e-9)
