"""Connection options of the Mammotion adapters (``ConnectionConfig.options``).

Options come from Fleet through the Device-Gateway (``MowerRegistration.connection_options``).

Secrets: in production Fleet stores a *reference* (``credential_ref``), never the password, and
the Device-Gateway resolves it from its own environment: ``env:NAME`` reads an environment
variable, ``file:/run/secrets/name`` a file (Docker or Kubernetes secrets). Phase 2 still
accepts a plain ``password`` option and logs a warning; TODO(phase 3): drop ``password`` once a
secret store exists (see ADR 0019).
"""

import base64
import binascii
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_log = logging.getLogger("mowdeck_control_mammotion.options")

_MAC: Final = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
DEFAULT_ESPHOME_PORT: Final = 6053
DEFAULT_REFRESH_SECONDS: Final = 20.0
"""Default of the cloud option ``refresh_seconds`` (fresh state requests, 10 to 300 s)."""
DEFAULT_REPORT_STREAM_SECONDS: Final = 60.0
"""Default of the cloud option ``report_stream_seconds`` (continuous report window while the
mower is active, 10 to 300 s, 0 off)."""
DEFAULT_ACTIVE_REFRESH_SECONDS: Final = 5.0
"""Default of the cloud option ``refresh_seconds_active`` (while mowing, 2 to 60 s)."""


class OptionsError(ValueError):
    """Invalid connection options. The message never contains secret values."""


def _str(options: Mapping[str, Any], key: str, *, required: bool = False) -> str | None:
    value = options.get(key)
    if value is None or value == "":
        if required:
            msg = f"option '{key}' is required"
            raise OptionsError(msg)
        return None
    if not isinstance(value, str):
        msg = f"option '{key}' must be a string"
        raise OptionsError(msg)
    return value.strip()


def _number(options: Mapping[str, Any], key: str, default: float, low: float, high: float) -> float:
    value = options.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"option '{key}' must be a number"
        raise OptionsError(msg)
    if not low <= float(value) <= high:
        msg = f"option '{key}' must be between {low} and {high}"
        raise OptionsError(msg)
    return float(value)


def _report_stream(options: Mapping[str, Any]) -> float:
    """``report_stream_seconds``: 0 (off) or 10 to 300."""
    value = options.get("report_stream_seconds", DEFAULT_REPORT_STREAM_SECONDS)
    if value == 0 and not isinstance(value, bool):
        return 0.0
    return _number(options, "report_stream_seconds", DEFAULT_REPORT_STREAM_SECONDS, 10.0, 300.0)


def _bool(options: Mapping[str, Any], key: str, default: bool) -> bool:
    value = options.get(key, default)
    if not isinstance(value, bool):
        msg = f"option '{key}' must be true or false"
        raise OptionsError(msg)
    return value


def _check_unknown(options: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(options) - allowed)
    if unknown:
        msg = f"unknown options: {', '.join(unknown)}"
        raise OptionsError(msg)


def resolve_secret(reference: str, environ: Mapping[str, str] | None = None) -> str:
    """Resolve ``env:NAME`` or ``file:/path`` to the secret value."""
    env = os.environ if environ is None else environ
    kind, _, target = reference.partition(":")
    if kind == "env" and target:
        value = env.get(target)
        if not value:
            msg = f"credential_ref: environment variable {target} is not set"
            raise OptionsError(msg)
        return value
    if kind == "file" and target:
        try:
            return Path(target).read_text(encoding="utf-8").strip()
        except OSError:
            msg = f"credential_ref: cannot read {target}"
            raise OptionsError(msg) from None
    msg = "credential_ref must look like env:NAME or file:/path"
    raise OptionsError(msg)


@dataclass(frozen=True, slots=True)
class CloudOptions:
    """Options of :class:`~mowdeck_control_mammotion.cloud.MammotionCloudConnection`."""

    account: str
    password: str
    device_name: str | None = None
    iot_id: str | None = None
    session_cache_path: Path | None = None
    map_sync_timeout_s: float = 300.0
    confirm_timeout_s: float = 30.0
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS
    refresh_seconds_active: float = DEFAULT_ACTIVE_REFRESH_SECONDS
    report_stream_seconds: float = DEFAULT_REPORT_STREAM_SECONDS
    all_cameras: bool = True
    """Ask the cloud for every camera of the mower (both front cameras of a LUBA 2 AWD X, the
    rear one of a Yuka) instead of the front left one only (``cameraStates`` of the stream
    token request); off when a mower misbehaves with more than one stream."""

    def __repr__(self) -> str:  # never print the password
        return (
            f"CloudOptions(account={self.account!r}, device_name={self.device_name!r}, "
            f"iot_id={self.iot_id!r})"
        )

    @classmethod
    def parse(
        cls, options: Mapping[str, Any], environ: Mapping[str, str] | None = None
    ) -> "CloudOptions":
        """Validate ``options`` and resolve the password (``credential_ref`` wins)."""
        _check_unknown(
            options,
            frozenset(
                {
                    "account",
                    "password",
                    "credential_ref",
                    "device_name",
                    "iot_id",
                    "session_cache_path",
                    "map_sync_timeout_s",
                    "confirm_timeout_s",
                    "refresh_seconds",
                    "refresh_seconds_active",
                    "report_stream_seconds",
                    "all_cameras",
                    # Set by Fleet for mowers of a linked account (ADR 0034); informational.
                    "linked_account_id",
                }
            ),
        )
        account = _str(options, "account", required=True)
        device_name = _str(options, "device_name")
        iot_id = _str(options, "iot_id")
        if device_name is None and iot_id is None:
            raise OptionsError("one of 'device_name' or 'iot_id' is required")
        reference = _str(options, "credential_ref")
        password = _str(options, "password")
        if reference is not None:
            password = resolve_secret(reference, environ)
        elif password is not None and "linked_account_id" in options:
            pass  # Fleet keeps the password of a linked account encrypted (ADR 0034)
        elif password is not None:
            _log.warning(
                "cloud connection for %s uses a plain 'password' option; store a "
                "'credential_ref' (env:NAME or file:/path) instead",
                device_name or iot_id,
            )
        else:
            raise OptionsError("one of 'credential_ref' or 'password' is required")
        cache = _str(options, "session_cache_path")
        assert account is not None  # noqa: S101 - required above
        return cls(
            account=account,
            password=password,
            device_name=device_name,
            iot_id=iot_id,
            session_cache_path=Path(cache) if cache else None,
            map_sync_timeout_s=_number(options, "map_sync_timeout_s", 300.0, 10.0, 1800.0),
            confirm_timeout_s=_number(options, "confirm_timeout_s", 30.0, 1.0, 300.0),
            refresh_seconds=_number(
                options, "refresh_seconds", DEFAULT_REFRESH_SECONDS, 10.0, 300.0
            ),
            refresh_seconds_active=_number(
                options, "refresh_seconds_active", DEFAULT_ACTIVE_REFRESH_SECONDS, 2.0, 60.0
            ),
            report_stream_seconds=_report_stream(options),
            all_cameras=_bool(options, "all_cameras", True),
        )


def _check_noise_psk(psk: str) -> None:
    try:
        decoded = base64.b64decode(psk, validate=True)
    except (binascii.Error, ValueError):
        decoded = b""
    if len(decoded) != 32:
        raise OptionsError("option 'noise_psk' must be the base64 ESPHome API key (32 bytes)")


@dataclass(frozen=True, slots=True)
class BleProxyOptions:
    """Options of :class:`~mowdeck_control_mammotion.ble.MammotionBleProxyConnection`."""

    host: str
    noise_psk: str
    ble_address: str
    port: int = DEFAULT_ESPHOME_PORT
    device_name: str | None = None
    allow_manual_blades: bool = False
    manual_blade_height_mm: int = 60
    manual_drive_speed_mps: float | None = None
    """``max_run_speed`` a drive session sends (ADR 0042); None: the mower's own speed setting
    for manual driving, capped at 1.0 m/s (see ``drive.manual_run_speed``)."""
    scan_timeout_s: float = 30.0
    map_sync_timeout_s: float = 300.0
    confirm_timeout_s: float = 30.0
    # Fresh-state requests, as for the cloud link: the mower does not report by itself.
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS
    refresh_seconds_active: float = DEFAULT_ACTIVE_REFRESH_SECONDS
    report_stream_seconds: float = DEFAULT_REPORT_STREAM_SECONDS
    fast_position: bool = True
    """Dual link only (ADR 0038, link roles): while mowing or returning the bridge also carries
    the continuous Bluetooth report stream (on top of the cloud polling)."""

    def __repr__(self) -> str:  # never print the key
        return (
            f"BleProxyOptions(host={self.host!r}, port={self.port}, "
            f"ble_address={self.ble_address!r}, device_name={self.device_name!r})"
        )

    @classmethod
    def parse(
        cls, options: Mapping[str, Any], environ: Mapping[str, str] | None = None
    ) -> "BleProxyOptions":
        """Validate ``options``; ``noise_psk`` may also be given as ``credential_ref``."""
        _check_unknown(
            options,
            frozenset(
                {
                    "host",
                    "port",
                    "noise_psk",
                    "credential_ref",
                    "ble_address",
                    "device_name",
                    "allow_manual_blades",
                    "manual_blade_height_mm",
                    "manual_drive_speed_mps",
                    "scan_timeout_s",
                    "map_sync_timeout_s",
                    "confirm_timeout_s",
                    "refresh_seconds",
                    "refresh_seconds_active",
                    "report_stream_seconds",
                    "fast_position",
                }
            ),
        )
        host = _str(options, "host", required=True)
        address = _str(options, "ble_address", required=True)
        assert host is not None and address is not None  # noqa: S101 - required above
        if not _MAC.match(address):
            raise OptionsError("option 'ble_address' must be a MAC address like AA:BB:CC:DD:EE:FF")
        reference = _str(options, "credential_ref")
        psk = resolve_secret(reference, environ) if reference else _str(options, "noise_psk")
        if psk is None:
            raise OptionsError("one of 'noise_psk' or 'credential_ref' is required")
        _check_noise_psk(psk)
        port = int(_number(options, "port", DEFAULT_ESPHOME_PORT, 1, 65535))
        return cls(
            host=host,
            noise_psk=psk,
            ble_address=address.upper(),
            port=port,
            device_name=_str(options, "device_name"),
            allow_manual_blades=_bool(options, "allow_manual_blades", False),
            manual_blade_height_mm=int(_number(options, "manual_blade_height_mm", 60, 20, 100)),
            manual_drive_speed_mps=(
                _number(options, "manual_drive_speed_mps", 1.0, 0.1, 1.0)
                if options.get("manual_drive_speed_mps") is not None
                else None
            ),
            scan_timeout_s=_number(options, "scan_timeout_s", 30.0, 1.0, 300.0),
            map_sync_timeout_s=_number(options, "map_sync_timeout_s", 300.0, 10.0, 1800.0),
            confirm_timeout_s=_number(options, "confirm_timeout_s", 30.0, 1.0, 300.0),
            refresh_seconds=_number(
                options, "refresh_seconds", DEFAULT_REFRESH_SECONDS, 10.0, 300.0
            ),
            refresh_seconds_active=_number(
                options, "refresh_seconds_active", DEFAULT_ACTIVE_REFRESH_SECONDS, 2.0, 60.0
            ),
            report_stream_seconds=_report_stream(options),
            fast_position=_bool(options, "fast_position", True),
        )
