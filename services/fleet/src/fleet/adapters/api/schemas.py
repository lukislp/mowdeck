"""Request and response bodies of the Fleet API (resource views stay in the service's
OpenAPI; the command bodies are shared contracts in ``mowdeck_contracts.v1.commands``)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from mowdeck_contracts.v1 import ConnectionKind, MowPattern
from pydantic import BaseModel, ConfigDict, Field

from fleet.application.linked_accounts import DeviceView, LinkedAccountView
from fleet.application.mowers import MowerView
from fleet.domain import models
from fleet.domain.connections import SCHEMAS, KindSchema, OptionSpec
from fleet.domain.models import Group, Site
from fleet.domain.rules import mask_connection_options


class In(BaseModel):
    """Base of request bodies: unknown fields are refused (no smuggled ``account_id``)."""

    model_config = ConfigDict(extra="forbid")


class Address(In):
    """Postal address of a site (all optional)."""

    street: str | None = Field(default=None, max_length=200)
    postal_code: str | None = Field(default=None, max_length=20)
    city: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=2, description="ISO 3166-1 alpha-2")


Name = Field(min_length=1, max_length=100)


Latitude = Field(default=None, ge=-90, le=90, description="WGS84 latitude in degrees")
Longitude = Field(default=None, ge=-180, le=180, description="WGS84 longitude in degrees")


class SiteIn(In):
    """``POST /sites``. ``latitude`` and ``longitude`` (both or none) locate the site for the
    Scheduler's rain pause."""

    name: str = Name
    timezone: str | None = Field(default=None, max_length=64, description="IANA name")
    address: Address | None = None
    latitude: float | None = Latitude
    longitude: float | None = Longitude


class SitePatch(In):
    """``PATCH /sites/{id}``: only the given fields change (``address: null`` clears it)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    address: Address | None = None
    latitude: float | None = Latitude
    longitude: float | None = Longitude


class SiteOut(BaseModel):
    """A site."""

    id: UUID
    name: str
    timezone: str
    is_default: bool
    address: dict[str, Any] | None
    latitude: float | None = None
    longitude: float | None = None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, item: Site) -> "SiteOut":
        """From the entity."""
        return cls(
            id=item.id,
            name=item.name,
            timezone=item.timezone,
            is_default=item.is_default,
            address=item.address,
            latitude=item.latitude,
            longitude=item.longitude,
            created_by=item.created_by,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


class SiteList(BaseModel):
    """``GET /sites``."""

    items: list[SiteOut]


class GroupIn(In):
    """``POST /groups``: ``site_id`` null or missing makes a cross-site group."""

    name: str = Name
    site_id: UUID | None = None


class GroupPatch(In):
    """``PATCH /groups/{id}``: ``site_id: null`` makes it a cross-site group."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    site_id: UUID | None = None


class GroupOut(BaseModel):
    """A group; ``mower_ids`` lists only the mowers the caller may view."""

    id: UUID
    name: str
    site_id: UUID | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    mower_ids: list[UUID] | None = None

    @classmethod
    def of(cls, item: Group, mower_ids: list[UUID] | None = None) -> "GroupOut":
        """From the entity."""
        return cls(
            id=item.id,
            name=item.name,
            site_id=item.site_id,
            created_by=item.created_by,
            created_at=item.created_at,
            updated_at=item.updated_at,
            mower_ids=mower_ids,
        )


class GroupList(BaseModel):
    """``GET /groups``."""

    items: list[GroupOut]


Options: Any = Field(
    default_factory=dict,
    description=(
        "Adapter specific (simulator: `time_scale`, `battery_percent`; cloud: `account`, "
        "`device_name` or `iot_id`, `credential_ref` or `password`; ble_bridge: `host`, `port`, "
        "`noise_psk`, `ble_address`). Secret values (`password`, `noise_psk`) are never "
        "returned: responses show `***`, and sending `***` back keeps the stored value."
    ),
)


class MowerIn(In):
    """``POST /mowers``: without ``site_id`` the mower goes to the account's default site.

    A ``cloud`` mower comes from a linked Mammotion account: ``linked_account_id`` and the
    ``device_name`` of its device list (``connection_options`` then hold only adapter extras
    such as ``refresh_seconds``); ``name`` defaults to the device name and ``model`` to the
    listed model."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    connection_kind: ConnectionKind
    linked_account_id: UUID | None = None
    device_name: str | None = Field(default=None, min_length=1, max_length=100)
    connection_options: dict[str, Any] = Options
    model: str | None = Field(default=None, max_length=100)
    serial: str | None = Field(default=None, min_length=1, max_length=64)
    site_id: UUID | None = None
    group_ids: list[UUID] = Field(default_factory=list, max_length=50)


class MowerPatch(In):
    """``PATCH /mowers/{id}``: only the given fields change."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    connection_kind: ConnectionKind | None = None
    connection_options: dict[str, Any] | None = None
    model: str | None = Field(default=None, max_length=100)
    serial: str | None = Field(default=None, min_length=1, max_length=64)
    site_id: UUID | None = None


class MowerGroupsIn(In):
    """``PUT /mowers/{id}/groups``: the complete set of groups."""

    group_ids: list[UUID] = Field(max_length=50)


class MowPatternIn(MowPattern):
    """``MowPattern`` as a request body: unknown keys are refused (422)."""

    model_config = ConfigDict(extra="forbid", frozen=True, title="MowPatternIn")


class MowDefaultsIn(In):
    """``PUT /mowers/{id}/mow-defaults``: the mowing defaults every client starts with.
    ``pattern: null`` drops them (clients then use stripes, 0 degrees, one border lap)."""

    pattern: MowPatternIn | None

    def to_domain(self) -> models.MowDefaults | None:
        """The domain value the use case keeps; None drops the defaults."""
        if self.pattern is None:
            return None
        return models.MowDefaults(pattern=MowPattern.model_validate(self.pattern.model_dump()))


class MowDefaults(BaseModel):
    """The saved mowing defaults of a mower (``MowerOut.mow_defaults``)."""

    pattern: MowPattern

    @classmethod
    def of(cls, saved: models.MowDefaults | None) -> "MowDefaults | None":
        """From the domain value; None when nothing is saved."""
        return cls(pattern=saved.pattern) if saved is not None else None


class MowerOut(BaseModel):
    """A mower."""

    id: UUID
    name: str
    model: str | None
    serial: str | None
    connection_kind: str
    connection_options: dict[str, Any]
    linked_account_id: UUID | None = Field(
        default=None, description="The linked account a `cloud` mower connects through."
    )
    iot_id: str | None = Field(
        default=None, description="The vendor's device id (unique across MowDeck accounts)."
    )
    site_id: UUID
    group_ids: list[UUID]
    area_names: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Custom names of map areas by area id (`MowerMap.areas[].id`); areas without one "
            "show the mower's own name, else `Area N` in map order."
        ),
    )
    mow_defaults: MowDefaults | None = Field(
        default=None,
        description=(
            "The mowing defaults every client starts with (`PUT /mowers/{id}/mow-defaults`); "
            "null until saved: then stripes at 0 degrees with one border lap."
        ),
    )
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, found: MowerView) -> "MowerOut":
        """From the use case view."""
        item = found.mower
        return cls(
            id=item.id,
            name=item.name,
            model=item.model,
            serial=item.serial,
            connection_kind=item.connection_kind,
            connection_options=mask_connection_options(item.connection_options),
            linked_account_id=item.linked_account_id,
            iot_id=item.iot_id,
            site_id=item.site_id,
            group_ids=found.group_ids,
            area_names=dict(found.area_names),
            mow_defaults=MowDefaults.of(item.mow_defaults),
            created_by=item.created_by,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


class AreaNameIn(In):
    """``PUT /mowers/{id}/area-names/{area_id}``: the custom name; ``null`` or blank drops it.
    Surrounding blanks are trimmed; at most 60 characters remain (422 otherwise)."""

    name: str | None = Field(max_length=200)


class AreaNamesOut(BaseModel):
    """The custom area names of a mower, by area id (areas without one are absent)."""

    names: dict[str, str]


class MowerList(BaseModel):
    """``GET /mowers``."""

    items: list[MowerOut]


class ConnectionOptionOut(BaseModel):
    """One option of a connection kind (the web app builds its form from these)."""

    key: str
    type: Literal["string", "integer", "number", "boolean"]
    title: str = Field(description="English label; the app translates the options it knows.")
    description: str
    required: bool
    secret: bool = Field(
        description="Write-only: answered as `***`; sending `***` back keeps the stored value."
    )
    group: str | None = Field(
        description="Options with the same group are alternatives: one of them is required."
    )
    advanced: bool
    pattern: str | None = Field(description="Regular expression a string value must match.")
    format_hint: str | None
    minimum: float | None
    maximum: float | None
    default: str | int | float | bool | None
    examples: list[str]
    managed: bool = Field(
        default=False,
        description="Set by Fleet from the linked account's device list: shown, not editable.",
    )

    @classmethod
    def of(cls, spec: OptionSpec) -> "ConnectionOptionOut":
        """From the domain schema."""
        return cls(
            key=spec.key,
            type=spec.type,
            title=spec.title,
            description=spec.description,
            required=spec.required,
            secret=spec.secret,
            group=spec.group,
            advanced=spec.advanced,
            pattern=spec.pattern,
            format_hint=spec.format_hint,
            minimum=spec.minimum,
            maximum=spec.maximum,
            default=spec.default,
            examples=list(spec.examples),
            managed=spec.managed,
        )


class ConnectionGroupOut(BaseModel):
    """A set of alternative options (e.g. password or credential reference)."""

    key: str
    title: str


class ConnectionKindOut(BaseModel):
    """A connection kind this installation accepts, with its option schema."""

    kind: str
    title: str
    description: str
    options: list[ConnectionOptionOut]
    groups: list[ConnectionGroupOut]
    extra_options: bool = Field(
        description="Keys outside `options` are accepted and kept (adapter specific)."
    )
    linked_account: bool = Field(
        default=False,
        description="Mowers of this kind come from a linked vendor account "
        "(`/linked-accounts`): register them with `linked_account_id` and `device_name`.",
    )

    @classmethod
    def of(cls, kind: str) -> "ConnectionKindOut":
        """The schema of ``kind`` (a kind without one accepts any options)."""
        schema = SCHEMAS.get(kind) or KindSchema(
            kind=kind, title=kind, description="", extra_options=True
        )
        return cls(
            kind=schema.kind,
            title=schema.title,
            description=schema.description,
            options=[ConnectionOptionOut.of(o) for o in schema.options],
            groups=[ConnectionGroupOut(key=k, title=t) for k, t in schema.groups.items()],
            extra_options=schema.extra_options,
            linked_account=schema.linked_account,
        )


class ConnectionKindList(BaseModel):
    """``GET /connection-kinds``: the kinds accepted here, in the order the app offers them."""

    items: list[ConnectionKindOut]


# --- linked accounts (ADR 0034) ------------------------------------------------------------


class LinkedAccountIn(In):
    """``POST /linked-accounts``: the Mammotion account to link (the only provider so far).
    The password is write-only."""

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256, json_schema_extra={"writeOnly": True})


class LinkedAccountSyncIn(In):
    """``POST /linked-accounts/{id}/sync``: optionally a new password (write-only)."""

    password: str | None = Field(
        default=None, min_length=1, max_length=256, json_schema_extra={"writeOnly": True}
    )


class LinkedAccountOut(BaseModel):
    """A linked account; the password is never returned."""

    id: UUID
    provider: str
    email: str
    created_at: datetime
    updated_at: datetime
    last_synced_at: datetime | None = Field(
        description="When the device list was last read successfully."
    )
    last_sync_error: str | None = Field(description="Why the last re-read failed, if it did.")
    device_count: int
    mower_count: int = Field(description="Mowers of this account that connect through it.")

    @classmethod
    def of(cls, view: LinkedAccountView) -> "LinkedAccountOut":
        """From the use case view."""
        item = view.account
        return cls(
            id=item.id,
            provider=item.provider,
            email=item.email,
            created_at=item.created_at,
            updated_at=item.updated_at,
            last_synced_at=item.last_synced_at,
            last_sync_error=item.last_sync_error,
            device_count=len(item.devices),
            mower_count=view.mower_count,
        )


class LinkedAccountList(BaseModel):
    """``GET /linked-accounts``."""

    items: list[LinkedAccountOut]


class LinkedDeviceOut(BaseModel):
    """A device of a linked account as its last device list showed it."""

    device_name: str
    model: str | None
    iot_id: str | None
    product_key: str | None
    online: bool | None
    is_mower: bool = Field(description="False for RTK base stations and other non-mowers.")
    registered: bool = Field(description="Registered as a mower of this account.")
    mower_id: UUID | None = Field(description="That mower, when `registered`.")
    registered_elsewhere: bool = Field(
        description="Registered in another MowDeck account (which one is not revealed); "
        "it cannot be added here."
    )

    @classmethod
    def of(cls, view: DeviceView) -> "LinkedDeviceOut":
        """From the use case view."""
        device = view.device
        return cls(
            device_name=device.device_name,
            model=device.model,
            iot_id=device.iot_id,
            product_key=device.product_key,
            online=device.online,
            is_mower=device.is_mower,
            registered=view.mower_id is not None,
            mower_id=view.mower_id,
            registered_elsewhere=view.elsewhere,
        )


class LinkedDeviceList(BaseModel):
    """``GET /linked-accounts/{id}/devices``."""

    items: list[LinkedDeviceOut]
    last_synced_at: datetime | None


class LinkedAccountDetail(LinkedAccountOut):
    """A linked account with its devices (answer of create and sync)."""

    devices: list[LinkedDeviceOut]


class LinkedMowerIn(In):
    """One mower of ``POST /linked-accounts/{id}/mowers``."""

    device_name: str = Field(min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    site_id: UUID | None = None
    group_ids: list[UUID] = Field(default_factory=list, max_length=50)
    serial: str | None = Field(default=None, min_length=1, max_length=64)


class LinkedMowersIn(In):
    """``POST /linked-accounts/{id}/mowers``: several devices of the account at once."""

    mowers: list[LinkedMowerIn] = Field(min_length=1, max_length=50)


class LinkedMowerFailure(BaseModel):
    """A device that could not be registered, with the problem it would have answered."""

    device_name: str
    status: int
    type: str
    detail: str


class LinkedMowersOut(BaseModel):
    """Answer of the bulk registration: created mowers and failures per device."""

    created: list[MowerOut]
    failed: list[LinkedMowerFailure]
