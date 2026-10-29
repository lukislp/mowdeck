"""Known devices and the new-login alert (ADR 0065).

Every login records the device it came from: a random id in the HttpOnly cookie
``mowdeck_device`` (the web app and the native app's web view keep it like any cookie), of which
only the SHA-256 is stored per user. A login whose device has no row of the user is a login from
a new device. The user is told (event ``identity.session.new_device``, Notifications sends
``security_new_login``) when

* new-login alerts are on (``users.new_login_alerts``, the default),
* the user already had another known device (the very first login, and the first one after
  this feature arrived, are recorded silently), and
* the login is not the native app taking over a browser login (``app_handoff``): the browser
  login that was handed over was itself checked a moment before.

A cleared cookie or a private window is a new device again. That is deliberate: no
fingerprinting, and an alert too many is better than one too few.
"""

import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from identity.application.context import RequestContext
from identity.application.ports import Tx
from identity.domain.credentials import sha256_hex
from identity.domain.devices import DeviceInfo, NetworkKind, describe_user_agent, ip_prefix
from identity.domain.models import KnownDevice, User

DEVICE_KEY = "device"
"""Key of the presented device cookie in :attr:`RequestContext.extra`."""
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
SILENT_METHODS = frozenset({"app_handoff"})
"""Login methods that never raise an alert (the browser login before it already did)."""


def new_device_token() -> str:
    """A random device id (256 bits, URL safe)."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class DeviceSeen:
    """The device of one login."""

    token: str
    """The device id for the cookie (the presented one, or a new one)."""
    device: KnownDevice
    info: DeviceInfo
    ip_prefix: str | None
    network: NetworkKind
    new_device: bool
    """No row of this user had this device id, and the user had other devices before."""
    alert: bool
    """Tell the user (see the module docstring)."""


async def remember_device(
    tx: Tx, user: User, ctx: RequestContext, now: datetime, method: str
) -> DeviceSeen:
    """Record the device of a login of ``user`` and decide about the alert."""
    presented = ctx.extra.get(DEVICE_KEY) or ""
    token = presented if _TOKEN.fullmatch(presented) else new_device_token()
    info = describe_user_agent(ctx.user_agent)
    prefix, network = ip_prefix(ctx.ip)
    token_hash = sha256_hex(token)
    known = await tx.known_devices.get(user_id=user.id, token_hash=token_hash)
    if known is not None:
        known.last_seen_at, known.last_ip_prefix = now, prefix
        await tx.known_devices.update(known)
        return DeviceSeen(token, known, info, prefix, network, new_device=False, alert=False)
    had_devices = await tx.known_devices.count(user_id=user.id) > 0
    device = KnownDevice(
        id=uuid4(),
        user_id=user.id,
        token_hash=token_hash,
        label=info.label[:100],
        first_seen_at=now,
        last_seen_at=now,
        last_ip_prefix=prefix,
    )
    await tx.known_devices.add(device)
    alert = had_devices and user.new_login_alerts and method not in SILENT_METHODS
    return DeviceSeen(token, device, info, prefix, network, new_device=had_devices, alert=alert)
