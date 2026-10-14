"""BLE through an ESPHome Bluetooth Proxy (extra ``esphome``), as in spike 0001 step 4.

The proxy runs the stock ESPHome ``bluetooth_proxy`` component. MowDeck connects to its native
API (TCP 6053, Noise encryption) with ``aioesphomeapi``, wraps it as a bleak scanner with
``bleak-esphome`` and registers that scanner with a minimal ``habluetooth`` manager (no Home
Assistant). The scanner yields a bleak ``BLEDevice`` for the mower's MAC, which PyMammotion's
``BLETransport`` connects through the proxy unchanged.

One ``habluetooth`` manager serves the whole process (``set_manager`` is global); every proxy
connection registers its own scanner and removes it again on close. The imports are lazy so
the package works without the extra.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("mowdeck_control_mammotion.esphome")

_manager: Any = None
_manager_lock = asyncio.Lock()


class EsphomeUnavailable(RuntimeError):  # noqa: N818 - reads like the other outcomes
    """The ``esphome`` extra is not installed."""


@dataclass(slots=True)
class ProxyLink:
    """An open proxy connection: the bleak ``BLEDevice`` of the mower plus a closer."""

    ble_device: Any
    advertised_name: str | None
    proxy_name: str
    close: Callable[[], Awaitable[None]]
    is_alive: Callable[[], bool]


async def _shared_manager() -> Any:
    global _manager  # one process wide manager, see the module docstring
    try:
        from bluetooth_adapters import get_adapters
        from habluetooth import BluetoothManager, set_manager
    except ImportError as exc:  # pragma: no cover - depends on the installation
        msg = "install mowdeck-control-mammotion[esphome] for the BLE proxy"
        raise EsphomeUnavailable(msg) from exc
    async with _manager_lock:
        if _manager is None:

            class _NoDiscoveryManager(BluetoothManager):
                def _discover_service_info(self, service_info: object) -> None:
                    return

            manager = _NoDiscoveryManager(get_adapters(), slot_manager=None)
            set_manager(manager)
            await manager.async_setup()
            install_bleak_wrappers()
            _manager = manager
    return _manager


def install_bleak_wrappers() -> None:
    """Route ``BleakClient`` and ``BleakScanner`` through habluetooth, i.e. the proxy.

    PyMammotion opens its Bluetooth link with a bare ``BleakClient(address)``. Without the
    wrappers bleak picks the platform backend (BlueZ in the container), which knows no such
    device and fails with ``KeyError('path')``; the wrappers resolve the address through the
    habluetooth manager, so the connection goes over the ESPHome proxy that saw the mower. This
    is what Home Assistant does at start-up (``install_multiple_bleak_catcher``). Modules that
    bound the names at import time, such as PyMammotion's ``bluetooth.ble``, are re-pointed as
    well. Safe to call more than once.
    """
    import sys

    from habluetooth import HaBleakClientWrapper, HaBleakScannerWrapper
    from habluetooth.usage import (
        ORIGINAL_BLEAK_CLIENT,
        ORIGINAL_BLEAK_RETRY_CONNECTOR_CLIENT_WITH_SERVICE_CACHE,
        ORIGINAL_BLEAK_SCANNER,
        HaBleakClientWithServiceCache,
        install_multiple_bleak_catcher,
    )

    install_multiple_bleak_catcher()
    # Names bound at import time (PyMammotion's transport uses
    # ``establish_connection(BleakClientWithServiceCache, ...)``): point them at the wrappers.
    replacements = (
        ("BleakClient", ORIGINAL_BLEAK_CLIENT, HaBleakClientWrapper),
        ("BleakScanner", ORIGINAL_BLEAK_SCANNER, HaBleakScannerWrapper),
        (
            "BleakClientWithServiceCache",
            ORIGINAL_BLEAK_RETRY_CONNECTOR_CLIENT_WITH_SERVICE_CACHE,
            HaBleakClientWithServiceCache,
        ),
    )
    for name, module in list(sys.modules.items()):
        if not name.startswith("pymammotion") or module is None:
            continue
        for attribute, original, wrapper in replacements:
            if getattr(module, attribute, None) is original:
                setattr(module, attribute, wrapper)  # noqa: B010


async def open_proxy(
    *, host: str, port: int, noise_psk: str, ble_address: str, scan_timeout_s: float
) -> ProxyLink:
    """Connect to the proxy and wait until it has seen ``ble_address``."""
    try:
        from aioesphomeapi.client import APIClient
        from bleak_esphome import connect_scanner
    except ImportError as exc:  # pragma: no cover - depends on the installation
        msg = "install mowdeck-control-mammotion[esphome] for the BLE proxy"
        raise EsphomeUnavailable(msg) from exc
    manager = await _shared_manager()
    api = APIClient(address=host, port=port, password=None, noise_psk=noise_psk)
    alive = {"api": False}

    async def on_stop(expected_disconnect: bool) -> None:
        alive["api"] = False
        _log.warning("ESPHome proxy %s disconnected (expected=%s)", host, expected_disconnect)

    await api.connect(on_stop=on_stop, login=True)
    alive["api"] = True
    unregister: Callable[[], None] | None = None
    unsetup: Callable[[], None] | None = None
    try:
        info = await api.device_info()
        data = connect_scanner(api, info, available=True)
        scanner = data.scanner
        if scanner is None:  # pragma: no cover - bleak-esphome always sets it
            msg = "bleak-esphome returned no scanner"
            raise RuntimeError(msg)
        unsetup = scanner.async_setup()  # synchronous, returns the teardown callback
        unregister = manager.async_register_scanner(scanner)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + scan_timeout_s
        found: tuple[Any, Any] | None = None
        while found is None and loop.time() < deadline:
            for device, adv in scanner.discovered_devices_and_advertisement_data.values():
                if str(device.address).upper() == ble_address.upper():
                    found = (device, adv)
            if found is None:
                await asyncio.sleep(0.5)
        if found is None:
            msg = f"{ble_address} not seen by proxy {info.name} within {scan_timeout_s:.0f} s"
            raise TimeoutError(msg)
    except BaseException:
        for undo in (unregister, unsetup):
            if undo is not None:
                undo()
        await api.disconnect()
        raise
    ble_device, adv = found
    advertised = getattr(adv, "local_name", None) or getattr(ble_device, "name", None)

    async def close() -> None:
        for undo in (unregister, unsetup):
            if undo is not None:
                undo()
        try:
            await api.disconnect()
        except Exception:  # closing must not fail the caller
            _log.warning("closing the ESPHome API connection failed", exc_info=True)
        alive["api"] = False

    _log.info("proxy %s sees %s (%s)", info.name, ble_address, advertised)
    return ProxyLink(
        ble_device=ble_device,
        advertised_name=str(advertised) if advertised else None,
        proxy_name=str(info.name),
        close=close,
        is_alive=lambda: alive["api"],
    )
