"""Devices of a Mammotion account: the discovery behind linked accounts (ADR 0034).

:func:`discover_devices` borrows the account's shared session from a
:class:`~mowdeck_control_mammotion.session.CloudSessionPool` (logging in only when no session for
these credentials is open, so mowers that are connected keep their session) and lists every
device PyMammotion registered at login: the mowers, and the RTK base stations and pool cleaners
that :func:`~mowdeck_control_mammotion.mapping.is_mower` marks as not registrable.

The list is what PyMammotion knows from its login: a device added in the Mammotion app later
appears once the session is opened again (no mower of the account connected for
``linger_seconds``, or a restart of the Device-Gateway).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mowdeck_contracts.v1 import DiscoveredDevice

from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.port import AuthError, MowerDevice, model_of
from mowdeck_control_mammotion.session import CloudSessionPool

_log = logging.getLogger("mowdeck_control_mammotion.discovery")

DeviceLister = Callable[[Any], list[DiscoveredDevice]]
"""Lists the devices of a logged-in client."""


class CloudLoginRefused(Exception):  # noqa: N818 - reads like the vendor's answer
    """The Mammotion cloud refused the credentials. The message never holds them."""


class CloudUnavailable(Exception):  # noqa: N818 - reads like the HTTP status it maps to
    """The Mammotion cloud did not answer or failed; the credentials may still be right."""


@dataclass(frozen=True, slots=True)
class Discovery:
    """The devices of an account and whether an open session answered."""

    devices: list[DiscoveredDevice]
    reused_session: bool


def _text(value: object, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def device_of(handle: Any) -> DiscoveredDevice:
    """One PyMammotion ``DeviceHandle`` as a :class:`DiscoveredDevice`."""
    name = str(handle.device_name)
    product_key = str(getattr(handle, "product_key", "") or "")
    raw = getattr(getattr(handle, "snapshot", None), "raw", None)
    try:
        model = model_of(raw, name, product_key)
    except Exception:  # the model is informational
        model = ""
    online = getattr(raw, "online", None)
    return DiscoveredDevice(
        device_name=name[:100],
        iot_id=_text(getattr(handle, "iot_id", None), 100),
        product_key=_text(product_key, 64),
        model=_text(model, 100),
        online=online if isinstance(online, bool) else None,
        is_mower=isinstance(raw, MowerDevice) and mapping.is_mower(name, product_key),
    )


def registry_devices(client: Any) -> list[DiscoveredDevice]:
    """Every device in the ``device_registry`` of a logged-in ``MammotionClient``."""
    return [device_of(handle) for handle in client.device_registry.all_devices]


async def discover_devices(
    pool: CloudSessionPool,
    account: str,
    password: str,
    *,
    lister: DeviceLister = registry_devices,
) -> Discovery:
    """The devices of the account, mowers first, each part sorted by name.

    Raises :class:`CloudLoginRefused` when the cloud refuses the credentials and
    :class:`CloudUnavailable` for every other failure.
    """
    try:
        lease = await pool.acquire(account, password)
    except AuthError as exc:
        raise CloudLoginRefused("the Mammotion cloud refused the e-mail or password") from exc
    except Exception as exc:
        # The error text may echo the request; only the type is logged and returned.
        _log.warning("Mammotion login for discovery failed: %s", type(exc).__name__)
        msg = f"the Mammotion cloud did not answer ({type(exc).__name__})"
        raise CloudUnavailable(msg) from exc
    try:
        devices = lister(lease.client)
    except Exception as exc:
        _log.warning("listing the Mammotion devices failed: %s", type(exc).__name__)
        msg = f"the Mammotion device list could not be read ({type(exc).__name__})"
        raise CloudUnavailable(msg) from exc
    finally:
        await lease.release()
    devices.sort(key=lambda d: (not d.is_mower, d.device_name.casefold()))
    return Discovery(devices=devices, reused_session=lease.reused)
