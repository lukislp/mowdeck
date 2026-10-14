"""Helpers for the Mammotion adapter tests (no conftest: imported explicitly)."""

import base64
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mowdeck_control import ConnectionConfig
from mowdeck_control_mammotion.ble import MammotionBleProxyConnection
from mowdeck_control_mammotion.cloud import MammotionCloudConnection
from mowdeck_control_mammotion.fake import FakeMammotionPort
from mowdeck_control_mammotion.options import BleProxyOptions, CloudOptions

MOWER_ID = UUID("00000000-0000-4000-8000-0000000000c1")
ACCOUNT_ID = UUID("00000000-0000-4000-8000-0000000000c2")
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
NOISE_PSK = base64.b64encode(bytes(range(32))).decode()

CLOUD_OPTIONS: dict[str, Any] = {
    "account": "second@example.com",
    "password": "secret-password",
    "device_name": "Luba-VPDRYRUN",
}
BLE_OPTIONS: dict[str, Any] = {
    "host": "mowdeck-bridge.local",
    "noise_psk": NOISE_PSK,
    "ble_address": "aa:bb:cc:dd:ee:ff",
    "device_name": "Luba-VPDRYRUN",
}


def config(options: dict[str, Any] | None = None) -> ConnectionConfig:
    """A connection config for the test mower."""
    return ConnectionConfig(mower_id=MOWER_ID, account_id=ACCOUNT_ID, options=options or {})


def clock() -> datetime:
    """A fixed clock."""
    return NOW


def cloud(
    port: FakeMammotionPort | None = None, **options: Any
) -> tuple[MammotionCloudConnection, FakeMammotionPort]:
    """A cloud connection on a fake port."""
    fake = port or FakeMammotionPort()
    parsed = CloudOptions.parse({**CLOUD_OPTIONS, **options})
    return MammotionCloudConnection(fake, config(), parsed, clock=clock), fake


def ble(
    port: FakeMammotionPort | None = None, **options: Any
) -> tuple[MammotionBleProxyConnection, FakeMammotionPort]:
    """A BLE proxy connection on a fake port."""
    fake = port or FakeMammotionPort()
    parsed = BleProxyOptions.parse({**BLE_OPTIONS, **options})
    return MammotionBleProxyConnection(fake, config(), parsed, clock=clock), fake
