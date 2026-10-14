"""The optional camera capability and the simulator's fake camera (ADR 0032)."""

from datetime import timedelta

import pytest
from mowdeck_contracts.v1 import CameraAccess, CameraPlacement, CameraProvider, CameraSession
from mowdeck_control import (
    CameraCapable,
    MowerConnection,
    NotConnected,
    SimulatedMower,
    SimulationConfig,
    camera_session_of,
)
from mowdeck_control.testing import ManualClock


async def test_simulator_camera_session_is_fake_and_expires_after_config() -> None:
    clock = ManualClock()
    sim = SimulatedMower(config=SimulationConfig(camera_session_seconds=60), clock=clock)
    await sim.connect()
    assert isinstance(sim, CameraCapable)
    assert (await sim.capabilities()).camera is CameraAccess.CLOUD
    session = await camera_session_of(sim)
    assert session is not None
    assert session.provider is CameraProvider.FAKE
    assert session.token == ""
    assert session.channel == f"sim-{sim.mower_id}"
    assert [(c.id, c.uid, c.placement) for c in session.cameras] == [
        ("front_left", 1, CameraPlacement.FRONT_LEFT),
        ("front_right", 2, CameraPlacement.FRONT_RIGHT),
    ]
    assert session.expires_at == clock() + timedelta(seconds=60)


async def test_simulator_without_camera_answers_none() -> None:
    sim = SimulatedMower(config=SimulationConfig(camera=False), clock=ManualClock())
    await sim.connect()
    assert (await sim.capabilities()).camera is CameraAccess.NONE
    assert await sim.get_camera_session() is None


async def test_simulator_camera_session_needs_a_connection() -> None:
    sim = SimulatedMower(clock=ManualClock())
    with pytest.raises(NotConnected):
        await sim.get_camera_session()


def test_simulator_rejects_out_of_range_camera_session_seconds() -> None:
    with pytest.raises(ValueError, match="camera_session_seconds"):
        SimulationConfig(camera_session_seconds=1)


async def test_camera_session_of_answers_none_for_connections_without_the_capability() -> None:
    class NoCamera:
        pass

    assert not isinstance(NoCamera(), CameraCapable)
    assert await camera_session_of(NoCamera()) is None


def test_camera_capable_is_separate_from_mower_connection() -> None:
    assert "get_camera_session" not in dir(MowerConnection)
    assert CameraSession.model_fields["token"].repr is False
