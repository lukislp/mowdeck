"""Public demo (ADR 0065): the gateway connects simulated mowers only."""

from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import ConnectionKind, MowerRegistration
from mowdeck_control import SimulatedMower

from device_gateway.adapters.control.connections import (
    RegistryConnectionProvider,
    SimulatedOnlyError,
)


def _registration(kind: ConnectionKind) -> MowerRegistration:
    return MowerRegistration(
        mower_id=uuid4(),
        account_id=uuid4(),
        name="Demo",
        connection_kind=kind,
        connection_options={"garden": "orchard"} if kind is ConnectionKind.SIMULATED else {},
    )


@pytest.mark.parametrize(
    "kind", [ConnectionKind.CLOUD, ConnectionKind.BLE_BRIDGE, ConnectionKind.WIFI]
)
def test_a_demo_gateway_refuses_every_real_mower(kind: ConnectionKind) -> None:
    def never(_: object) -> None:
        raise AssertionError("the cloud factory must not be called")

    provider = RegistryConnectionProvider(simulated_only=True, cloud_factory=never)  # type: ignore[arg-type]
    with pytest.raises(SimulatedOnlyError, match="demo mode"):
        provider.create(_registration(kind))


def test_a_demo_gateway_connects_simulated_mowers_with_their_garden() -> None:
    provider = RegistryConnectionProvider(simulated_only=True)
    connection = provider.create(_registration(ConnectionKind.SIMULATED))
    assert isinstance(connection, SimulatedMower)
    assert connection.config.garden == "orchard"
