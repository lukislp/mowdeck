"""Connection registry and entry point discovery."""

import logging
from importlib.metadata import EntryPoint
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import ConnectionKind
from mowdeck_control import (
    ENTRY_POINT_GROUP,
    ConnectionConfig,
    ConnectionRegistry,
    MowerConnection,
    SimulatedMower,
    UnknownConnectionKind,
    available_kinds,
    create_connection,
    create_simulated_connection,
    default_registry,
)

CONFIG = ConnectionConfig(mower_id=uuid4(), account_id=uuid4())


def fake_cloud_factory(config: ConnectionConfig) -> MowerConnection:
    """Stands in for an adapter from another package."""
    return SimulatedMower(mower_id=config.mower_id, account_id=config.account_id)


NOT_CALLABLE = 42


def _entry_point(name: str, value: str) -> EntryPoint:
    return EntryPoint(name=name, value=value, group=ENTRY_POINT_GROUP)


def test_simulated_kind_is_registered_by_this_package() -> None:
    assert ConnectionKind.SIMULATED in available_kinds()
    connection = create_connection("simulated", CONFIG)
    assert isinstance(connection, SimulatedMower)
    assert isinstance(connection, MowerConnection)
    assert connection.mower_id == CONFIG.mower_id
    assert default_registry().factory(ConnectionKind.SIMULATED) is create_simulated_connection


def test_simulated_kind_is_also_an_installed_entry_point() -> None:
    registry = ConnectionRegistry()
    assert ConnectionKind.SIMULATED in registry.load_entry_points()
    assert registry.factory(ConnectionKind.SIMULATED) is create_simulated_connection


def test_unknown_kind_raises() -> None:
    registry = ConnectionRegistry(entry_point_source=list)
    with pytest.raises(UnknownConnectionKind, match="no connection adapter"):
        registry.create(ConnectionKind.BLE_BRIDGE, CONFIG)
    with pytest.raises(UnknownConnectionKind, match="unknown connection kind 'telepathy'"):
        registry.factory("telepathy")
    with pytest.raises(LookupError):
        create_connection(ConnectionKind.WIFI, CONFIG)


def test_entry_point_discovery_with_fake_entry_points(caplog: pytest.LogCaptureFixture) -> None:
    module = __name__
    entry_points = [
        _entry_point("cloud", f"{module}:fake_cloud_factory"),
        _entry_point("hologram", f"{module}:fake_cloud_factory"),
        _entry_point("wifi", "mowdeck_missing_package:factory"),
        _entry_point("ble_bridge", f"{module}:NOT_CALLABLE"),
    ]
    registry = ConnectionRegistry(entry_point_source=lambda: entry_points)
    with caplog.at_level(logging.INFO, logger="mowdeck_control.registry"):
        assert registry.kinds() == [ConnectionKind.CLOUD]
    assert "unknown connection kind" in caplog.text
    assert "cannot load" in caplog.text
    assert "not callable" in caplog.text
    connection = registry.create("cloud", CONFIG)
    assert isinstance(connection, SimulatedMower)


def test_entry_points_are_loaded_lazily_and_code_registration_wins() -> None:
    calls: list[int] = []

    def source() -> list[EntryPoint]:
        calls.append(1)
        return [_entry_point("cloud", f"{__name__}:fake_cloud_factory")]

    registry = ConnectionRegistry(entry_point_source=source)
    registry.register(ConnectionKind.CLOUD, create_simulated_connection)
    assert registry.factory("cloud") is create_simulated_connection
    assert calls == []
    assert registry.kinds() == [ConnectionKind.CLOUD]
    assert calls == [1]
    assert registry.factory("cloud") is create_simulated_connection
    registry.kinds()
    assert calls == [1]


def test_register_conflicts() -> None:
    registry = ConnectionRegistry(entry_point_source=list)
    registry.register("cloud", fake_cloud_factory)
    registry.register("cloud", fake_cloud_factory)  # same factory again: no-op
    with pytest.raises(ValueError, match="already registered"):
        registry.register("cloud", create_simulated_connection)
    registry.register("cloud", create_simulated_connection, replace=True)
    assert registry.factory("cloud") is create_simulated_connection
    with pytest.raises(ValueError):
        registry.register("telepathy", fake_cloud_factory)
