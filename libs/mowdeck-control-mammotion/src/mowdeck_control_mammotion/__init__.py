"""MowDeck adapter for Mammotion mowers (LUBA, Yuka) on top of PyMammotion. GPL-3.0-only.

This package is GPL-3.0 because it imports PyMammotion (GPL-3.0), see ADR 0011. It plugs into
``mowdeck-control`` through the entry point group ``mowdeck.connections``:

* ``cloud``: :class:`MammotionCloudConnection` (Mammotion cloud, MQTT), factory
  :func:`create_cloud_connection`.
* ``ble_bridge``: :class:`MammotionBleProxyConnection` (BLE through an ESPHome Bluetooth Proxy,
  extra ``esphome``), factory :func:`create_ble_bridge_connection`.
* a ``cloud`` registration with a ``ble_bridge`` link (ADR 0038) gets a
  :class:`MammotionDualConnection`: the cloud plus the bridge for manual driving.

``wifi`` is not implemented: Mammotion restores control over Wi-Fi only from firmware
1.30.31.19 (spike 0001, section 3.1).
"""

from mowdeck_control_mammotion.base import MammotionBase
from mowdeck_control_mammotion.ble import (
    BleProxyPort,
    MammotionBleProxyConnection,
    create_ble_bridge_connection,
)
from mowdeck_control_mammotion.cloud import MammotionCloudConnection, create_cloud_connection
from mowdeck_control_mammotion.drive import BleDriveSession
from mowdeck_control_mammotion.dual import BridgeWatcher, MammotionDualConnection
from mowdeck_control_mammotion.options import BleProxyOptions, CloudOptions, OptionsError
from mowdeck_control_mammotion.port import ClientPort, MammotionPort

__version__ = "0.1.0"
PYMAMMOTION_VERSION = "0.10.9"
"""The exact PyMammotion release this adapter was written and tested against."""

__all__ = [
    "PYMAMMOTION_VERSION",
    "BleDriveSession",
    "BleProxyOptions",
    "BleProxyPort",
    "BridgeWatcher",
    "ClientPort",
    "CloudOptions",
    "MammotionBase",
    "MammotionBleProxyConnection",
    "MammotionCloudConnection",
    "MammotionDualConnection",
    "MammotionPort",
    "OptionsError",
    "create_ble_bridge_connection",
    "create_cloud_connection",
]
