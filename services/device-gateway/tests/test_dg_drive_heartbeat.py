"""Telemetry heartbeat while a drive session is open (AI assist watches it every few seconds)."""

import asyncio
from datetime import timedelta

from dg_support import envelope, make_app, registration, telemetry, wait_until
from mowdeck_contracts.v1 import ManualDrive
from mowdeck_service_kit.bus import InMemoryBus, publish_command
from mowdeck_service_kit.testing import lifespan_client


async def test_dg_drive_session_publishes_a_fast_heartbeat() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(
        bus=bus,
        bootstrap_mowers=[reg.model_dump(mode="json")],
        telemetry_heartbeat_seconds=30.0,
        telemetry_drive_heartbeat_seconds=0.2,
        sim_tick_seconds=60.0,  # the simulator itself changes nothing in this test
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        await asyncio.sleep(0.5)
        idle = len(telemetry(bus, reg.mower_id))
        await asyncio.sleep(0.6)
        assert len(telemetry(bus, reg.mower_id)) == idle, "no heartbeat without a drive session"
        for _ in range(6):  # standing still: a zero frame keeps the drive session open
            frame = ManualDrive(linear=0.0, angular=0.0, ttl_ms=1000)
            await publish_command(bus, envelope(reg, frame, ttl=timedelta(seconds=1)))
            await asyncio.sleep(0.2)
        assert len(telemetry(bus, reg.mower_id)) - idle >= 3
