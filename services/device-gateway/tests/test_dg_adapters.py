"""The real mower adapters are installed with the gateway and resolve through the registry."""

import base64
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import ConnectionKind, MowerRegistration
from mowdeck_control import ConnectionRegistry

from device_gateway.adapters.control.connections import RegistryConnectionProvider


def _registration(kind: ConnectionKind, options: dict[str, object]) -> MowerRegistration:
    return MowerRegistration(
        mower_id=uuid4(),
        account_id=uuid4(),
        name="LUBA",
        connection_kind=kind,
        connection_options=options,
    )


def test_dg_registry_resolves_the_mammotion_cloud_and_ble_bridge_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DG_TEST_MAMMOTION_PASSWORD", "from-the-gateway-environment")
    provider = RegistryConnectionProvider(registry=ConnectionRegistry())
    cloud = provider.create(
        _registration(
            ConnectionKind.CLOUD,
            {
                "account": "second@example.com",
                "credential_ref": "env:DG_TEST_MAMMOTION_PASSWORD",
                "device_name": "Luba-VP1234",
            },
        )
    )
    ble = provider.create(
        _registration(
            ConnectionKind.BLE_BRIDGE,
            {
                "host": "mowdeck-bridge.local",
                "noise_psk": base64.b64encode(bytes(32)).decode(),
                "ble_address": "AA:BB:CC:DD:EE:FF",
            },
        )
    )
    assert cloud.kind is ConnectionKind.CLOUD
    assert type(cloud).__name__ == "MammotionCloudConnection"
    assert ble.kind is ConnectionKind.BLE_BRIDGE
    assert type(ble).__name__ == "MammotionBleProxyConnection"
    assert not cloud.is_connected and not ble.is_connected
    assert provider.background_jobs(cloud) == []
    kinds = ConnectionRegistry().kinds()
    assert {ConnectionKind.CLOUD, ConnectionKind.BLE_BRIDGE, ConnectionKind.SIMULATED} <= set(kinds)
