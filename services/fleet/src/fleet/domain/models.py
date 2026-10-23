"""Entities of Fleet. Field names equal the column names in ``adapters.db.tables``.

Properties ending in ``_key`` are not stored; the memory store uses them for the unique rules
that PostgreSQL expresses as (partial) unique indexes.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import MowPattern


@dataclass(slots=True)
class Site:
    """A place with mowers (a garden, a sports ground). Every account has one hidden default
    site; mowers registered without a site go there."""

    id: UUID
    account_id: UUID
    name: str
    timezone: str
    is_default: bool
    created_at: datetime
    updated_at: datetime
    address: dict[str, Any] | None = None
    created_by: UUID | None = None
    latitude: float | None = None
    longitude: float | None = None

    @property
    def default_key(self) -> UUID | None:
        """Unique per account among default sites (``uq_sites_one_default``)."""
        return self.account_id if self.is_default else None

    @property
    def name_key(self) -> str | None:
        """Unique per account among named sites (``uq_sites_account_name``)."""
        return None if self.is_default else self.name.casefold()


@dataclass(slots=True)
class Group:
    """A set of mowers, inside one site or across the sites of the account (``site_id`` None)."""

    id: UUID
    account_id: UUID
    name: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    site_id: UUID | None = None

    @property
    def name_key(self) -> str:
        """Unique per account (``uq_groups_account_name``)."""
        return self.name.casefold()


@dataclass(frozen=True, slots=True)
class MowDefaults:
    """The mowing defaults every client starts with (web start sheet, Home Assistant, the
    Scheduler): the pattern settings of a native start. Set with
    ``PUT /mowers/{id}/mow-defaults`` (ADR 0030, amendment 2026-10-14); nothing is sent to the
    mower. How the value is stored is the database adapter's business (``adapters.db``)."""

    pattern: MowPattern


@dataclass(slots=True)
class Mower:
    """A registered mower. It always belongs to one site and to any number of groups."""

    id: UUID
    account_id: UUID
    site_id: UUID
    name: str
    connection_kind: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    connection_options: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    serial: str | None = None
    linked_account_id: UUID | None = None
    """The linked vendor account a ``cloud`` mower connects through (ADR 0034); its
    credentials are merged into the registration the Device-Gateway receives."""
    iot_id: str | None = None
    """The vendor's stable device id (Mammotion IoT id) of a cloud mower: one physical mower
    can be registered in one MowDeck account only, installation-wide (``uq_mowers_iot_id``)."""
    mow_defaults: MowDefaults | None = None
    """The mowing defaults every client starts with; None until someone saved them."""
    base_station_id: UUID | None = None
    """The RTK base station the mower takes its corrections from (ADR 0047); None without."""


@dataclass(slots=True)
class BaseStation:
    """An RTK base station of the account (ADR 0047), linked to the mowers that use it.

    It has no connection of its own: the Device-Gateway reports it through the connection of a
    linked mower. ``source`` ``cloud``: a device of a linked vendor account (``linked_account_id``,
    ``device_name`` and ``iot_id`` from its device list; one physical base station in one
    MowDeck account, ``uq_base_stations_iot_id``); ``simulated``: the simulator's base station
    for simulated mowers (no device fields).
    """

    id: UUID
    account_id: UUID
    name: str
    source: str
    created_at: datetime
    updated_at: datetime
    linked_account_id: UUID | None = None
    device_name: str | None = None
    iot_id: str | None = None
    model: str | None = None
    created_by: UUID | None = None


@dataclass(slots=True)
class LinkedAccount:
    """A vendor cloud account (Mammotion) linked to a MowDeck account (ADR 0034).

    The password is stored encrypted (AES-256-GCM, ``MOWDECK_CREDENTIAL_ENCRYPTION_KEY``) and
    never returned. ``devices`` caches the device list of the last successful discovery
    (``DiscoveredDevice`` as JSON).
    """

    id: UUID
    account_id: UUID
    provider: str
    email: str
    password_encrypted: bytes
    created_at: datetime
    updated_at: datetime
    created_by: UUID | None = None
    devices: list[dict[str, Any]] = field(default_factory=list)
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None

    @property
    def email_key(self) -> tuple[str, str]:
        """Unique per account (``uq_linked_accounts_account_email``, ``lower(email)``)."""
        return (self.provider, self.email.lower())


@dataclass(slots=True)
class MowerLink:
    """An additional link of a mower next to its connection (ADR 0038): today a Bluetooth
    bridge (``kind`` ``ble_bridge``) next to a cloud connection, one row per mower and kind.

    ``options`` are the public options of the link (host, port, Bluetooth address, device name,
    blade settings); ``secrets_encrypted`` is the AES-256-GCM blob (``adapters.crypto``, the
    link id as context) of a JSON object with the withheld options (``{"noise_psk": ...}``;
    extensible). ``proxy_info`` caches what the last scan learnt about the bridge
    (``ProxyInfo`` as JSON) and is None until a scan answered.
    """

    id: UUID
    mower_id: UUID
    account_id: UUID
    kind: str
    secrets_encrypted: bytes
    created_at: datetime
    updated_at: datetime
    options: dict[str, Any] = field(default_factory=dict)
    proxy_info: dict[str, Any] | None = None
    created_by: UUID | None = None

    @property
    def pair_key(self) -> tuple[UUID, str]:
        """Unique (``uq_mower_links_mower_kind``)."""
        return (self.mower_id, self.kind)


@dataclass(slots=True)
class MowerGroup:
    """Membership of a mower in a group."""

    id: UUID
    mower_id: UUID
    group_id: UUID
    account_id: UUID

    @property
    def pair_key(self) -> tuple[UUID, UUID]:
        """Unique (``uq_mower_groups_pair``)."""
        return (self.mower_id, self.group_id)


@dataclass(slots=True)
class MowerAreaName:
    """A custom name of one map area of a mower (the mower keeps its own names; MowDeck never
    writes them to the mower). Key: ``(mower_id, area_id)``."""

    mower_id: UUID
    area_id: str
    account_id: UUID
    name: str
    updated_at: datetime
    updated_by: UUID | None = None

    @property
    def pair_key(self) -> tuple[UUID, str]:
        """The primary key (``pk_mower_area_names``)."""
        return (self.mower_id, self.area_id)


@dataclass(slots=True)
class CommandRecord:
    """Who sent what to which mower, when, and what became of it (the command audit)."""

    command_id: UUID
    account_id: UUID
    mower_id: UUID
    kind: str
    command: dict[str, Any]
    priority: str
    issued_by_subject: UUID
    issued_by: dict[str, Any]
    issued_at: datetime
    expires_at: datetime
    status: str
    correlation_id: str
    bulk_id: UUID | None = None
    reason: str | None = None
    result_at: datetime | None = None
    origin: str | None = None
    schedule_id: UUID | None = None
    plan_hash: str | None = None


@dataclass(slots=True)
class DriveSession:
    """A manual drive session (joystick over Realtime, ADR 0026): who drove which mower, when,
    how it ended and how many frames went out. Frames themselves are not stored."""

    id: UUID
    account_id: UUID
    mower_id: UUID
    started_by_subject: UUID
    started_by: dict[str, Any]
    started_at: datetime
    expires_at: datetime
    blade_on_allowed: bool = False
    connection_id: UUID | None = None
    ended_at: datetime | None = None
    end_reason: str | None = None
    frames: int | None = None
    frames_dropped: int | None = None
    origin: str | None = None
    """Service that drove on behalf of the subject (``vision``); None for the joystick."""


@dataclass(slots=True)
class AuditEntry:
    """An append-only record of a change to sites, groups or mowers."""

    at: datetime
    action: str
    account_id: UUID
    target_type: str
    target_id: str
    id: int | None = None
    actor_subject: str | None = None
    actor_kind: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    event_id: UUID | None = None
    """Id of the event this row records (unique): a redelivered event writes no second row."""
