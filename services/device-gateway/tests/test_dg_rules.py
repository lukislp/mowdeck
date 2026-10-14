"""Pure rules, settings and the connection provider."""

import itertools
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from dg_support import envelope, make_settings, registration
from mowdeck_contracts.v1 import ConnectionKind, MowerState, StartMow
from mowdeck_control import SimulatedMower, UnknownConnectionKind
from pydantic import ValidationError

from device_gateway.adapters.control.connections import RegistryConnectionProvider
from device_gateway.domain.rules import (
    Shard,
    backoff,
    command_rejection,
    is_significant_change,
)
from device_gateway.settings import Settings


def test_shard_ownership_partitions_all_mowers() -> None:
    shards = [Shard(i, 3) for i in range(3)]
    for mower_id in (uuid4() for _ in range(50)):
        assert sum(shard.owns(mower_id) for shard in shards) == 1
    assert Shard().owns(uuid4())
    assert Shard(1, 2).owns(UUID(int=7))
    assert not Shard(0, 2).owns(UUID(int=7))
    with pytest.raises(ValueError, match="invalid shard"):
        Shard(2, 2)
    with pytest.raises(ValueError, match="invalid shard"):
        Shard(0, 0)


def test_command_rejection_reasons() -> None:
    reg = registration()
    command = envelope(reg, StartMow(area_ids=["front"]))
    assert command_rejection(reg, command) is None
    assert command_rejection(None, command) == "unknown mower"
    other_mower = registration(account_id=reg.account_id)
    assert command_rejection(other_mower, command) == "command is for another mower"
    foreign = envelope(reg, StartMow(area_ids=["front"]), account_id=uuid4())
    assert command_rejection(reg, foreign) == "the command's account does not own this mower"


def test_significant_change_ignores_timestamp_and_raw() -> None:
    state = SimulatedMower().snapshot()
    later = state.model_copy(
        update={"observed_at": state.observed_at + timedelta(seconds=5), "raw": {"tick": 1}}
    )
    assert is_significant_change(None, state)
    assert not is_significant_change(state, later)
    assert is_significant_change(state, later.model_copy(update={"battery_percent": 50}))
    assert isinstance(later, MowerState)


def test_backoff_grows_to_the_cap() -> None:
    assert list(itertools.islice(backoff(1, 5), 5)) == [1, 2, 4, 5, 5]
    with pytest.raises(ValueError, match="backoff"):
        next(backoff(0, 1))
    with pytest.raises(ValueError, match="backoff"):
        next(backoff(2, 1))


def test_settings_defaults_and_validation() -> None:
    settings = Settings.model_validate({})
    assert settings.service_name == "device-gateway"
    assert (settings.shard_index, settings.shard_count) == (0, 1)
    assert settings.telemetry_heartbeat_seconds == 10
    assert settings.sim_time_scale == 1.0
    assert settings.command_consumer == "device-gateway-0"
    assert make_settings(shard_index=2, shard_count=3).command_consumer == "device-gateway-2"
    with pytest.raises(ValidationError, match="SHARD_INDEX"):
        Settings.model_validate({"shard_index": 1, "shard_count": 1})


def test_bootstrap_mowers_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = registration(options={"time_scale": 30})
    monkeypatch.setenv("MOWDECK_BOOTSTRAP_MOWERS", f"[{reg.model_dump_json()}]")
    monkeypatch.setenv("MOWDECK_SIM_TIME_SCALE", "60")
    monkeypatch.setenv("MOWDECK_SHARD_COUNT", "4")
    monkeypatch.setenv("MOWDECK_SHARD_INDEX", "3")
    settings = Settings.from_env()
    assert settings.bootstrap_mowers == [reg]
    assert (settings.sim_time_scale, settings.shard_index, settings.shard_count) == (60, 3, 4)


def test_provider_applies_the_time_scale_and_ticks_simulators() -> None:
    provider = RegistryConnectionProvider(sim_time_scale=60, sim_tick_seconds=0.5)
    default = provider.create(registration())
    explicit = provider.create(registration(options={"time_scale": 5}))
    assert isinstance(default, SimulatedMower)
    assert isinstance(explicit, SimulatedMower)
    assert default.config.time_scale == 60
    assert explicit.config.time_scale == 5
    assert len(provider.background_jobs(default)) == 1
    assert provider.background_jobs(object()) == []  # type: ignore[arg-type]
    # cloud is installed (mowdeck-control-mammotion) and refuses empty options; wifi has no
    # adapter at all
    cloud = registration().model_copy(update={"connection_kind": ConnectionKind.CLOUD})
    with pytest.raises(ValueError, match="account"):
        provider.create(cloud)
    wifi = registration().model_copy(update={"connection_kind": ConnectionKind.WIFI})
    with pytest.raises(UnknownConnectionKind):
        provider.create(wifi)
