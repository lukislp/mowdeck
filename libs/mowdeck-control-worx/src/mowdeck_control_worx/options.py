"""Connection options of a Worx mower (``ConnectionConfig.options``).

The gateway merges the linked account's credential fields (``brand``, ``email``, ``password``)
into the mower's options; Fleet sets ``iot_id`` (the mower's serial number) and ``device_name``
from the device list. ``offer_zones`` and ``command_timeout_s`` are the mower options the
descriptor declares; every other option (``cloud_provider``, the drive calibration, ...) is
ignored.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from mowdeck_control_worx.descriptor import (
    BRAND,
    BRANDS,
    COMMAND_TIMEOUT,
    DEFAULT_BRAND,
    DEFAULT_COMMAND_TIMEOUT_S,
    EMAIL,
    MAX_COMMAND_TIMEOUT_S,
    MIN_COMMAND_TIMEOUT_S,
    OFFER_ZONES,
    PASSWORD,
)
from mowdeck_control_worx.session import WorxLogin


def _text(options: Mapping[str, Any], key: str) -> str | None:
    value = options.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"option {key} must be a string"
        raise ValueError(msg)
    return value.strip() or None


def login_of(credentials: Mapping[str, Any]) -> WorxLogin:
    """The login in ``credentials`` (a linked account's fields); raises ``ValueError`` that
    never names a credential value."""
    email = _text(credentials, EMAIL)
    password = credentials.get(PASSWORD)
    brand = _text(credentials, BRAND) or DEFAULT_BRAND
    if email is None or not isinstance(password, str) or not password:
        msg = "the linked account's e-mail address and password are missing"
        raise ValueError(msg)
    if brand not in BRANDS:
        msg = f"unknown brand; use one of {', '.join(BRANDS)}"
        raise ValueError(msg)
    return WorxLogin(brand=brand, email=email, password=password)


@dataclass(frozen=True, slots=True)
class WorxOptions:
    """The parsed options; the login keeps the password out of ``repr``."""

    login: WorxLogin = field(repr=False)
    serial_number: str | None = None
    device_name: str | None = None
    offer_zones: bool = True
    command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S

    @property
    def brand_name(self) -> str:
        """The brand as users know it (``Worx Landroid``, ``Kress``, ``LandXcape``)."""
        return BRANDS[self.login.brand]

    @classmethod
    def parse(cls, options: Mapping[str, Any]) -> Self:
        """Raises ``ValueError`` (never naming a credential value) for unusable options."""
        login = login_of(options)
        serial = _text(options, "iot_id")
        device_name = _text(options, "device_name")
        if serial is None and device_name is None:
            msg = "iot_id or device_name is needed to pick the mower"
            raise ValueError(msg)
        zones = options.get(OFFER_ZONES, True)
        if not isinstance(zones, bool):
            msg = f"option {OFFER_ZONES} must be true or false"
            raise ValueError(msg)
        timeout = options.get(COMMAND_TIMEOUT, DEFAULT_COMMAND_TIMEOUT_S)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float):
            msg = f"option {COMMAND_TIMEOUT} must be a number"
            raise ValueError(msg)
        if not MIN_COMMAND_TIMEOUT_S <= timeout <= MAX_COMMAND_TIMEOUT_S:
            msg = (
                f"option {COMMAND_TIMEOUT} must lie within "
                f"{MIN_COMMAND_TIMEOUT_S:g}..{MAX_COMMAND_TIMEOUT_S:g}"
            )
            raise ValueError(msg)
        return cls(
            login=login,
            serial_number=serial,
            device_name=device_name,
            offer_zones=zones,
            command_timeout_s=float(timeout),
        )
