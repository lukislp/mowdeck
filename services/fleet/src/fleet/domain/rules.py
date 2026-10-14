"""Rules of Fleet without I/O: which right a command needs, how long it lives, how its record
advances, which shard a mower belongs to, what a connection may carry."""

import json
import re
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

from mowdeck_contracts.v1 import CommandState

from fleet.domain.connections import SCHEMAS, KindSchema, option_problems
from fleet.domain.errors import FieldProblem, InvalidInput

CAN_VIEW: Final = "can_view"
CAN_CONTROL: Final = "can_control"
CAN_DRIVE: Final = "can_drive"
CAN_EMERGENCY_STOP: Final = "can_emergency_stop"
CAN_BULK_COMMAND: Final = "can_bulk_command"
CAN_MANAGE_MOWERS: Final = "can_manage_mowers"

COMMAND_RELATION: Final[Mapping[str, str]] = {
    "start_mow": CAN_CONTROL,
    "pause_mow": CAN_CONTROL,
    "resume_mow": CAN_CONTROL,
    "return_to_dock": CAN_CONTROL,
    "set_blade_height": CAN_CONTROL,
    "set_speed": CAN_CONTROL,
    "set_light": CAN_CONTROL,
    "manual_drive": CAN_DRIVE,
    "emergency_stop": CAN_EMERGENCY_STOP,  # equals can_view in the model: whoever sees may stop
}
"""The computed relation a principal needs on the mower to send a command of each kind."""


def relation_for(kind: str) -> str:
    """The relation needed for ``kind``; ``ValueError`` for an unknown kind (never allow)."""
    try:
        return COMMAND_RELATION[kind]
    except KeyError:
        msg = f"unknown command kind {kind!r}"
        raise ValueError(msg) from None


def bulk_gate_relation(kind: str) -> str:
    """The relation needed on the site or group of a bulk command.

    An emergency stop needs only what it needs on a single mower (to see it), so anybody who
    sees a group can stop all of it; every other bulk command needs ``can_bulk_command``.
    """
    return CAN_EMERGENCY_STOP if relation_for(kind) == CAN_EMERGENCY_STOP else CAN_BULK_COMMAND


@dataclass(frozen=True, slots=True)
class CommandLimits:
    """Expiry settings of commands (from ``Settings``)."""

    default_ttl: timedelta = timedelta(seconds=30)
    max_ttl: timedelta = timedelta(seconds=300)
    emergency_stop_ttl: timedelta = timedelta(seconds=10)
    manual_drive_ttl: timedelta = timedelta(seconds=2)


def command_ttl(kind: str, requested_seconds: int | None, limits: CommandLimits) -> timedelta:
    """How long a command may wait for delivery before the gateway drops it.

    Emergency stop and manual drive always use their own short expiry (a late stop or a late
    joystick frame is worse than none); other kinds use the requested value capped at
    ``max_ttl``, or the default.
    """
    if kind == "emergency_stop":
        return limits.emergency_stop_ttl
    if kind == "manual_drive":
        return limits.manual_drive_ttl
    if requested_seconds is None:
        return min(limits.default_ttl, limits.max_ttl)
    return min(timedelta(seconds=requested_seconds), limits.max_ttl)


STATUS_RANK: Final[Mapping[str, int]] = {
    CommandState.PENDING.value: 0,
    CommandState.ACCEPTED.value: 1,
    CommandState.REJECTED.value: 2,
    CommandState.EXECUTED.value: 2,
    CommandState.EXPIRED.value: 2,
    CommandState.FAILED.value: 2,
}
"""Order of command statuses; a record only moves to a higher rank."""


def advances_from(status: str) -> frozenset[str]:
    """The statuses a record may have for ``status`` to be applied (all lower ranks).

    Redelivered and out-of-order results are harmless: ``accepted`` after ``executed`` finds no
    record in a lower status and changes nothing.
    """
    rank = STATUS_RANK[status]
    return frozenset(s for s, r in STATUS_RANK.items() if r < rank)


def in_shard(mower_id: UUID, shard_index: int, shard_count: int) -> bool:
    """The Device-Gateway rule: mower ``m`` belongs to shard ``m.int % count``."""
    return mower_id.int % shard_count == shard_index


_SECRET_KEY = re.compile(r"pass|secret|token|key|credential|psk", re.IGNORECASE)

SECRET_MASK: Final = "***"  # noqa: S105 - the placeholder, not a secret
"""Shown instead of a secret option value in API responses."""

ALLOWED_SECRET_OPTIONS: Final[Mapping[str, frozenset[str]]] = {
    "cloud": frozenset({"password", "credential_ref"}),
    "ble_bridge": frozenset({"noise_psk", "credential_ref"}),
}
"""Top-level option keys that may look like secrets, per connection kind.

TODO(phase 3): drop ``password`` (and ``noise_psk``) once a secret store exists; production
registrations should already use ``credential_ref`` (``env:NAME`` or ``file:/path``, resolved by
the Device-Gateway), so Fleet stores a reference to the secret rather than the secret itself.
"""

MASKED_OPTIONS: Final = frozenset({"password", "noise_psk"})
"""Option keys whose values are secrets: never echoed by the API (``credential_ref`` is a
reference and is shown)."""


def _secret_keys(value: Any, path: str = "", allowed: frozenset[str] = frozenset()) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if _SECRET_KEY.search(str(key)) and not (path == "" and key in allowed):
                found.append(here)
            found.extend(_secret_keys(item, here))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_secret_keys(item, f"{path}[{index}]"))
    return found


def _non_empty_string(options: Mapping[str, Any], key: str) -> bool:
    value = options.get(key)
    return isinstance(value, str) and bool(value.strip())


def _check_kind_options(
    kind: str, options: Mapping[str, Any], schema: KindSchema | None = None
) -> None:
    """The option schema of ``kind`` (:data:`fleet.domain.connections.SCHEMAS`), if any."""
    schema = schema or SCHEMAS.get(kind)
    if schema is None:
        return
    problems = option_problems(schema, options)
    if problems:
        msg = f"connection_options for {kind}: {'; '.join(p.message for p in problems)}"
        raise InvalidInput(
            msg,
            fields=[
                FieldProblem(f"connection_options/{p.key}", p.message, p.code) for p in problems
            ],
        )


def validate_connection_options(
    options: Mapping[str, Any],
    max_bytes: int,
    kind: str | None = None,
    schema: KindSchema | None = None,
) -> dict[str, Any]:
    """Connection options are adapter specific and small; only known secret keys are allowed.

    Raises ``InvalidInput`` when the JSON form exceeds ``max_bytes``, a key looks like a secret
    (``pass``, ``secret``, ``token``, ``key``, ``credential``, ``psk``, at any depth) and is not
    one the ``kind`` needs (:data:`ALLOWED_SECRET_OPTIONS`, top level only), or the options
    break the kind's schema (required options, alternatives, types, ranges, patterns; the
    error then names each option in ``fields``).
    """
    size = len(json.dumps(options, separators=(",", ":")).encode("utf-8"))
    if size > max_bytes:
        msg = f"connection_options must not exceed {max_bytes} bytes"
        raise InvalidInput(msg)
    allowed = ALLOWED_SECRET_OPTIONS.get(kind or "", frozenset())
    secret = _secret_keys(options, allowed=allowed)
    if secret:
        msg = f"connection_options must not contain secrets (keys: {', '.join(secret[:5])})"
        raise InvalidInput(msg)
    if kind is not None:
        _check_kind_options(kind, options, schema)
    return dict(options)


def plain_secret_options(options: Mapping[str, Any]) -> list[str]:
    """Secret values stored in plain text (for a warning; ``credential_ref`` is preferred)."""
    return sorted(key for key in MASKED_OPTIONS if _non_empty_string(options, key))


def mask_connection_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of ``options`` safe to return from the API: secret values are :data:`SECRET_MASK`."""
    return {
        key: SECRET_MASK if key in MASKED_OPTIONS and value not in (None, "") else value
        for key, value in options.items()
    }


def keep_masked_secrets(new: Mapping[str, Any], stored: Mapping[str, Any]) -> dict[str, Any]:
    """Replace :data:`SECRET_MASK` placeholders in ``new`` by the stored secret values.

    A client that reads a mower (masked) and sends the options back unchanged must not
    overwrite the secret with the mask.
    """
    merged = dict(new)
    for key in MASKED_OPTIONS:
        if merged.get(key) == SECRET_MASK and key in stored:
            merged[key] = stored[key]
    return merged


def validate_timezone(name: str) -> str:
    """An IANA time zone name such as ``Europe/Berlin``; ``InvalidInput`` otherwise."""
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        msg = f"unknown time zone {name!r}"
        raise InvalidInput(msg) from None
    return name


def validate_coordinates(
    latitude: float | None, longitude: float | None
) -> tuple[float | None, float | None]:
    """WGS84 coordinates of a site: both or none, latitude -90..90, longitude -180..180."""
    if (latitude is None) != (longitude is None):
        msg = "latitude and longitude must be given together"
        raise InvalidInput(msg)
    if latitude is not None and not -90 <= latitude <= 90:
        msg = "latitude must be between -90 and 90"
        raise InvalidInput(msg)
    if longitude is not None and not -180 <= longitude <= 180:
        msg = "longitude must be between -180 and 180"
        raise InvalidInput(msg)
    return latitude, longitude


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$")


def validate_email(value: str) -> str:
    """An e-mail address (surrounding blanks stripped): one ``@``, a dotted domain, at most 254
    characters, no blanks or control characters inside; ``InvalidInput`` otherwise. Compared
    case-insensitively with ``lower()`` (like the database index)."""
    address = value.strip()
    bad = any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in address)
    if bad or len(address) > 254 or not _EMAIL.match(address):
        msg = "email must be an e-mail address"
        raise InvalidInput(msg, fields=[FieldProblem("email", msg, "format")])
    return address


def clean_name(name: str, what: str) -> str:
    """Strip surrounding blanks; 1 to 100 characters remain (``InvalidInput`` otherwise)."""
    cleaned = name.strip()
    if not 1 <= len(cleaned) <= 100:
        msg = f"{what} name must have 1 to 100 characters"
        raise InvalidInput(msg)
    return cleaned
