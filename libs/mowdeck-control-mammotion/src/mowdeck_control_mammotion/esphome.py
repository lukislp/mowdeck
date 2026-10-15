"""BLE through an ESPHome Bluetooth Proxy (extra ``esphome``), as in spike 0001 step 4.

The proxy runs the stock ESPHome ``bluetooth_proxy`` component. MowDeck connects to its native
API (TCP 6053, Noise encryption) with ``aioesphomeapi``, wraps it as a bleak scanner with
``bleak-esphome`` and registers that scanner with a minimal ``habluetooth`` manager (no Home
Assistant). The scanner yields a bleak ``BLEDevice`` for the mower's MAC, which PyMammotion's
``BLETransport`` connects through the proxy unchanged.

One ``habluetooth`` manager serves the whole process (``set_manager`` is global); every proxy
connection registers its own scanner and removes it again on close. The imports are lazy so
the package works without the extra.

Bridge firmware (ADR 0038): the proxy's ``device_info`` names its ESPHome release and, when the
firmware was built from MowDeck's template, the ``esphome.project`` (``mowdeck.bridge`` and the
template version). :func:`proxy_info_of` turns it into a neutral
:class:`~mowdeck_contracts.v1.ProxyInfo` and marks the bridge ``outdated`` when the release is
older than :data:`BRIDGE_MIN_ESPHOME`, the template older than :data:`BRIDGE_TEMPLATE_VERSION`
or the project missing (``no_project``: a bridge flashed from the stock proxy YAML). A scan
(:func:`scan_proxy`) connects to the proxy, reads that information and collects the mower
advertisements (``Luba-*``, ``Yuka-*``) until the expected name shows up.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from mowdeck_contracts.v1 import (
    BridgeScanError,
    BridgeScanErrorCode,
    BridgeScanReply,
    ProxyInfo,
    ScannedDevice,
    utc_now,
)
from packaging.version import InvalidVersion, Version

_log = logging.getLogger("mowdeck_control_mammotion.esphome")

_manager: Any = None
_manager_lock = asyncio.Lock()

BRIDGE_PROJECT_NAME: Final = "mowdeck.bridge"
"""``esphome.project.name`` of a bridge built from MowDeck's template."""
BRIDGE_TEMPLATE_VERSION: Final = "1"
"""``esphome.project.version`` the current template writes; an older one counts as outdated."""
BRIDGE_MIN_ESPHOME: Final = "2025.2.0"
"""Oldest ESPHome release the bridge may run (the ``bluetooth_proxy`` API ``aioesphomeapi``
45 speaks); older firmware counts as outdated."""
MOWER_NAME_PREFIXES: Final = ("Luba-", "Yuka-")
"""Bluetooth names of Mammotion mowers (the advertised name equals the cloud device name)."""
OUTDATED_NO_PROJECT: Final = "no_project"
OUTDATED_ESPHOME: Final = "esphome_version"
OUTDATED_TEMPLATE: Final = "project_version"
_SCAN_POLL_S: Final = 0.5


class EsphomeUnavailable(RuntimeError):  # noqa: N818 - reads like the other outcomes
    """The ``esphome`` extra is not installed."""


@dataclass(slots=True)
class ProxyLink:
    """An open proxy connection: the bleak ``BLEDevice`` of the mower plus a closer.

    ``device_info`` is the proxy's ESPHome ``device_info`` (see :func:`proxy_info_of`) and
    ``scanner`` the bleak scanner on the proxy, so an open link can report the mower's RSSI
    (:meth:`rssi`) and answer a scan from the advertisements it sees (:meth:`seen_devices`).
    """

    ble_device: Any
    advertised_name: str | None
    proxy_name: str
    close: Callable[[], Awaitable[None]]
    is_alive: Callable[[], bool]
    device_info: Any = None
    scanner: Any = None
    host: str = ""
    port: int = 0
    address: str = ""
    _rssi: int | None = field(default=None, repr=False)

    def rssi(self) -> int | None:
        """The mower's signal strength at the proxy from its latest advertisement (dBm), or
        the value seen when the link opened; None when the scanner never saw one."""
        if self.scanner is not None:
            seen = _advertisements(self.scanner)
            for device, adv in seen:
                if _same_address(device, self.address):
                    rssi = _rssi_of(adv, device)
                    if rssi is not None:
                        self._rssi = rssi
                    break
        return self._rssi

    def seen_devices(self) -> list[ScannedDevice]:
        """The mower advertisements the proxy's scanner currently holds."""
        if self.scanner is None:
            return []
        return _mower_devices(_advertisements(self.scanner))


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
                setattr(module, attribute, wrapper)


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

    closed = {"done": False}

    async def close() -> None:
        """Remove the scanner from habluetooth, then end the API connection (the proxy drops
        its Bluetooth connections with it). Idempotent; a failing step never skips the next."""
        if closed["done"]:
            return
        closed["done"] = True
        for undo in (unregister, unsetup):
            if undo is not None:
                try:
                    undo()
                except Exception:
                    _log.warning("removing the scanner of proxy %s failed", host, exc_info=True)
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
        device_info=info,
        scanner=scanner,
        host=host,
        port=port,
        address=ble_address.upper(),
        _rssi=_rssi_of(adv, ble_device),
    )


# --- Bridge firmware and scans (ADR 0038) -------------------------------------------------------


def _version(text: str | None) -> Version | None:
    if not text:
        return None
    try:
        return Version(str(text).strip().split("-")[0])
    except InvalidVersion:
        return None


def outdated_reason(
    esphome_version: str | None, project_name: str | None, project_version: str | None
) -> str | None:
    """Why the bridge firmware counts as outdated, or None when it is current.

    No ``esphome.project`` (stock proxy YAML) is :data:`OUTDATED_NO_PROJECT`; a release below
    :data:`BRIDGE_MIN_ESPHOME` is :data:`OUTDATED_ESPHOME`; a MowDeck template older than
    :data:`BRIDGE_TEMPLATE_VERSION` is :data:`OUTDATED_TEMPLATE`. A project of another name is
    left alone (someone built their own firmware on purpose).
    """
    release = _version(esphome_version)
    minimum = _version(BRIDGE_MIN_ESPHOME)
    if release is not None and minimum is not None and release < minimum:
        return OUTDATED_ESPHOME
    if not project_name:
        return OUTDATED_NO_PROJECT
    if project_name == BRIDGE_PROJECT_NAME:
        current, wanted = _version(project_version), _version(BRIDGE_TEMPLATE_VERSION)
        if current is None or (wanted is not None and current < wanted):
            return OUTDATED_TEMPLATE
    return None


def proxy_info_of(device_info: Any, *, checked_at: datetime | None = None) -> ProxyInfo:
    """The neutral description of a bridge from ESPHome's ``device_info`` (any object with the
    attributes ``name``, ``esphome_version``, ``compilation_time``, ``project_name``,
    ``project_version`` and ``bluetooth_mac_address``; missing ones read as unknown)."""

    def text(name: str) -> str | None:
        value = getattr(device_info, name, None)
        return str(value).strip()[:64] or None if value else None

    project_name = text("project_name")
    project_version = text("project_version")
    esphome_version = text("esphome_version")
    reason = outdated_reason(esphome_version, project_name, project_version)
    mac = text("bluetooth_mac_address") or text("mac_address")
    return ProxyInfo(
        proxy_name=(text("name") or "bridge")[:100],
        esphome_version=esphome_version,
        compiled_at=text("compilation_time"),
        project_name=project_name,
        project_version=project_version,
        bluetooth_mac=mac[:32] if mac else None,
        checked_at=checked_at or utc_now(),
        outdated=reason is not None,
        outdated_reason=reason,
    )


def scan_error_code(exc: BaseException) -> BridgeScanErrorCode:
    """The neutral code of a failure while talking to the proxy, by the exception's class
    (the names of ``aioesphomeapi.core``, matched without importing the extra): a refused
    encryption key is ``auth_failed``, every other connection problem ``unreachable``."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & {"InvalidEncryptionKeyAPIError", "RequiresEncryptionAPIError", "BadNameAPIError"}:
        return BridgeScanErrorCode.AUTH_FAILED
    return BridgeScanErrorCode.UNREACHABLE


def is_mower_name(name: str | None) -> bool:
    """True for the Bluetooth name of a Mammotion mower."""
    return bool(name) and str(name).startswith(MOWER_NAME_PREFIXES)


def _advertisements(scanner: Any) -> list[tuple[Any, Any]]:
    try:
        return list(scanner.discovered_devices_and_advertisement_data.values())
    except Exception:
        return []


def _same_address(device: Any, address: str) -> bool:
    return str(getattr(device, "address", "")).upper() == address.upper()


def _rssi_of(adv: Any, device: Any) -> int | None:
    value = getattr(adv, "rssi", None)
    if value is None:
        value = getattr(device, "rssi", None)
    try:
        rssi = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return rssi if rssi is not None and -128 <= rssi <= 20 else None


def _mower_devices(seen: list[tuple[Any, Any]]) -> list[ScannedDevice]:
    devices: list[ScannedDevice] = []
    for device, adv in seen:
        name = getattr(adv, "local_name", None) or getattr(device, "name", None)
        address = str(getattr(device, "address", "") or "")
        if not is_mower_name(name) or not address:
            continue
        rssi = _rssi_of(adv, device)
        devices.append(
            ScannedDevice(
                name=str(name)[:100], address=address[:32], rssi=-128 if rssi is None else rssi
            )
        )
    devices.sort(key=lambda d: -d.rssi)
    return devices[:50]


def scan_reply_from(
    proxy: ProxyInfo | None,
    devices: list[ScannedDevice],
    expected_name: str | None,
    *,
    detail: str,
) -> BridgeScanReply:
    """A reply from what a scanner holds: ``timeout`` when ``expected_name`` is not among the
    devices (the proxy itself answered, so ``proxy`` stays set)."""
    if expected_name is not None and all(d.name != expected_name for d in devices):
        return BridgeScanReply(
            proxy=proxy,
            devices=devices,
            error=BridgeScanError(code=BridgeScanErrorCode.TIMEOUT, detail=detail[:500]),
        )
    return BridgeScanReply(proxy=proxy, devices=devices)


async def scan_proxy(
    *,
    host: str,
    port: int,
    noise_psk: str,
    expected_name: str | None,
    timeout_s: float,
    clock: Callable[[], datetime] = utc_now,
) -> BridgeScanReply:
    """Connect to the proxy, read its ``device_info`` and collect mower advertisements until
    ``expected_name`` shows up or ``timeout_s`` passes (ADR 0038).

    Never raises for a bridge problem: connection errors become ``unreachable``, a refused key
    ``auth_failed`` (:func:`scan_error_code`), a mower that did not advertise ``timeout`` with
    the proxy information and whatever else was seen. The key is used, never logged. Only one
    native API client should talk to a proxy at a time; the caller serialises scans per host.
    """
    try:
        from aioesphomeapi.client import APIClient
        from bleak_esphome import connect_scanner
    except ImportError as exc:  # pragma: no cover - depends on the installation
        msg = "install mowdeck-control-mammotion[esphome] for the BLE proxy"
        raise EsphomeUnavailable(msg) from exc
    manager = await _shared_manager()
    api = APIClient(address=host, port=port, password=None, noise_psk=noise_psk)
    try:
        async with asyncio.timeout(timeout_s):
            await api.connect(login=True)
            info = await api.device_info()
    except (TimeoutError, Exception) as exc:
        code = (
            BridgeScanErrorCode.UNREACHABLE
            if isinstance(exc, TimeoutError)
            else scan_error_code(exc)
        )
        _log.info("bridge scan of %s:%s failed: %s", host, port, type(exc).__name__)
        with contextlib.suppress(Exception):
            await api.disconnect()
        return BridgeScanReply(
            error=BridgeScanError(
                code=code, detail=f"proxy {host}:{port}: {type(exc).__name__}"[:500]
            )
        )
    proxy = proxy_info_of(info, checked_at=clock())
    unregister: Callable[[], None] | None = None
    unsetup: Callable[[], None] | None = None
    try:
        data = connect_scanner(api, info, available=True)
        scanner = data.scanner
        if scanner is None:  # pragma: no cover - bleak-esphome always sets it
            msg = "bleak-esphome returned no scanner"
            raise RuntimeError(msg)
        unsetup = scanner.async_setup()
        unregister = manager.async_register_scanner(scanner)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        devices: list[ScannedDevice] = []
        while True:
            devices = _mower_devices(_advertisements(scanner))
            if expected_name is not None and any(d.name == expected_name for d in devices):
                break
            if loop.time() >= deadline:
                break
            await asyncio.sleep(_SCAN_POLL_S)
    finally:
        for undo in (unregister, unsetup):
            if undo is not None:
                with contextlib.suppress(Exception):
                    undo()
        with contextlib.suppress(Exception):
            await api.disconnect()
    _log.info("bridge scan of %s saw %d mower(s)", proxy.proxy_name, len(devices))
    return scan_reply_from(
        proxy,
        devices,
        expected_name,
        detail=f"{expected_name} did not advertise at {proxy.proxy_name} within {timeout_s:.0f} s",
    )
