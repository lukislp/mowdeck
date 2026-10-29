"""Request and response bodies of the Fleet API (resource views stay in the service's
OpenAPI; the command bodies are shared contracts in ``mowdeck_contracts.v1.commands``)."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from mowdeck_contracts.v1 import (
    PROVIDER_ID_PATTERN,
    CameraTransport,
    ConnectionKind,
    CredentialFieldKind,
    DeviceType,
    DriveCalibration,
    MowPattern,
    ProviderDescriptor,
    ProxyInfo,
    text_for,
)
from pydantic import BaseModel, ConfigDict, Field

from fleet.application.base_stations import BaseStationView
from fleet.application.linked_accounts import CredentialVault, DeviceView, LinkedAccountView
from fleet.application.links import ScanInput, drive_calibration_of, proxy_info_of
from fleet.application.mowers import MowerView
from fleet.domain import models
from fleet.domain.connections import SCHEMAS, KindSchema, OptionSpec
from fleet.domain.models import Group, MowerLink, Site
from fleet.domain.rules import SECRET_MASK, mask_connection_options, mask_link_options


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

    A ``cloud`` mower comes from a linked vendor account: ``linked_account_id`` and the
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


# --- additional links (ADR 0038) -----------------------------------------------------------


class BridgeLinkIn(In):
    """``PUT /mowers/{id}/links/ble_bridge``: a Bluetooth bridge (an ESPHome Bluetooth proxy)
    next to the mower's connection. ``noise_psk`` is write-only; ``***`` keeps the stored key."""

    host: str = Field(min_length=1, max_length=253, description="Host name or IP of the bridge.")
    port: int = Field(default=6053, ge=1, le=65535, description="The bridge's API port.")
    noise_psk: str = Field(
        min_length=1,
        max_length=256,
        json_schema_extra={"writeOnly": True},
        description="The bridge's API encryption key (`api.encryption.key` of an ESPHome "
        "bridge); `***` keeps the stored key.",
    )
    ble_address: str = Field(
        min_length=1, max_length=32, description="The mower's Bluetooth address (from the scan)."
    )
    device_name: str | None = Field(
        default=None,
        max_length=100,
        description="The mower's Bluetooth name (from the scan); the cloud device name when "
        "omitted.",
    )
    allow_manual_blades: bool = Field(
        default=False, description="Blades may run while the mower is driven manually."
    )
    manual_blade_height_mm: int | None = Field(
        default=None, ge=10, le=120, description="Blade height for manual driving."
    )
    manual_drive_speed_mps: float | None = Field(
        default=None,
        ge=0.1,
        le=1.0,
        description="`max_run_speed` the mower gets when a drive session opens (ADR 0042); "
        "omitted: the mower's own speed setting for manual driving, at most 1.0 m/s.",
    )

    def to_options(self) -> dict[str, Any]:
        """The link options the use case validates with the `ble_bridge` schema."""
        options = self.model_dump(exclude_none=True)
        if not options.get("device_name"):
            options.pop("device_name", None)
        return options


class BridgeScanIn(In):
    """``POST /mowers/{id}/links/ble_bridge/scan``: where the bridge is and its key (`***`: the
    stored key of the existing link)."""

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=6053, ge=1, le=65535)
    noise_psk: str = Field(
        default=SECRET_MASK,
        min_length=1,
        max_length=256,
        json_schema_extra={"writeOnly": True},
        description="The bridge's API encryption key; `***` means the stored key.",
    )

    def to_input(self) -> ScanInput:
        """The use case input."""
        return ScanInput(host=self.host, port=self.port, noise_psk=self.noise_psk)


class LinkOut(BaseModel):
    """An additional link of a mower (``MowerOut.links[kind]``, the link routes)."""

    kind: str
    options: dict[str, Any] = Field(
        description="The link's options (`host`, `port`, `ble_address`, `device_name`, "
        "`allow_manual_blades`, `manual_blade_height_mm`, `manual_drive_speed_mps` and the "
        "managed drive calibration keys); secret keys show `***` and sending "
        "`***` back keeps the stored value."
    )
    proxy_info: ProxyInfo | None = Field(
        default=None,
        description="What the last scan or check learnt about the bridge; null until one "
        "answered. The live link state is `MowerState.links[kind]` from telemetry.",
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, link: MowerLink) -> "LinkOut":
        """From the entity (never the secrets)."""
        return cls(
            kind=link.kind,
            options=mask_link_options(link.kind, link.options),
            proxy_info=proxy_info_of(link),
            created_at=link.created_at,
            updated_at=link.updated_at,
        )


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
    links: dict[str, LinkOut] = Field(
        default_factory=dict,
        description="Additional links next to the connection by kind (`ble_bridge`: a Bluetooth "
        "bridge, `PUT /mowers/{id}/links/ble_bridge`); empty for a mower with only its "
        "connection.",
    )
    drive_calibration: DriveCalibration | None = Field(
        default=None,
        description="The stored drive calibration (`PUT /mowers/{id}/drive-calibration`): from "
        "the `ble_bridge` link, else from the connection options; null until one is stored.",
    )
    base_station_id: UUID | None = Field(
        default=None,
        description="The RTK base station the mower is linked to (`PUT /mowers/{id}/base-station`"
        ", ADR 0047); its state is in the mower's telemetry (`MowerState.base_station`).",
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
            links={link.kind: LinkOut.of(link) for link in found.links},
            drive_calibration=drive_calibration_of(found.links, item.connection_options),
            base_station_id=item.base_station_id,
            created_by=item.created_by,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


class CameraSessionIn(In):
    """``POST /mowers/{id}/camera/session`` (optional body): how the client wants the video."""

    transport: CameraTransport = Field(
        default=CameraTransport.AUTO,
        description=(
            "`auto` (default): the camera relay, the direct vendor path only when the relay "
            "cannot serve the mower's camera provider or is not deployed; `relay`: only the "
            "relay, its errors are answered; `direct`: the vendor session (ADR 0032)."
        ),
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
        description="Set by Fleet (from the linked account's device list, or a stored drive "
        "calibration): shown, not editable.",
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
    provider_options: dict[str, list[ConnectionOptionOut]] = Field(
        default_factory=dict,
        description="For a kind with `linked_account`: the further options a mower takes, by "
        "its provider (the option `cloud_provider`), as the enabled providers declare them "
        "(ADR 0066). A provider without settings has an empty list.",
    )

    @classmethod
    def of(
        cls, kind: str, providers: Mapping[str, Sequence[OptionSpec]] | None = None
    ) -> "ConnectionKindOut":
        """The schema of ``kind`` (a kind without one accepts any options); ``providers``: the
        options of each enabled cloud provider, for a kind of linked accounts."""
        schema = SCHEMAS.get(kind) or KindSchema(
            kind=kind, title=kind, description="", extra_options=True
        )
        provider_options = (
            {p: [ConnectionOptionOut.of(o) for o in specs] for p, specs in providers.items()}
            if schema.linked_account and providers
            else {}
        )
        return cls(
            kind=schema.kind,
            title=schema.title,
            description=schema.description,
            options=[ConnectionOptionOut.of(o) for o in schema.options],
            groups=[ConnectionGroupOut(key=k, title=t) for k, t in schema.groups.items()],
            extra_options=schema.extra_options,
            linked_account=schema.linked_account,
            provider_options=provider_options,
        )


class ConnectionKindList(BaseModel):
    """``GET /connection-kinds``: the kinds accepted here, in the order the app offers them."""

    items: list[ConnectionKindOut]


# --- linked accounts (ADR 0034) ------------------------------------------------------------


class LinkedAccountIn(In):
    """``POST /linked-accounts``: the vendor account to link, with the credential fields of its
    provider (``GET /linked-accounts/providers``). Secret fields are write-only."""

    provider: str = Field(pattern=PROVIDER_ID_PATTERN, description="The provider id.")
    credentials: dict[str, str] = Field(
        max_length=10,
        description="Every field of the provider's descriptor by key.",
        json_schema_extra={"writeOnly": True},
    )


class LinkedAccountSyncIn(In):
    """``POST /linked-accounts/{id}/sync``: optionally new secret fields (write-only), e.g. after
    the vendor revoked a password or a key; the stored values stay for the missing ones."""

    credentials: dict[str, str] | None = Field(
        default=None,
        max_length=10,
        description="New values of secret fields of the provider by key.",
        json_schema_extra={"writeOnly": True},
    )
    password: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Deprecated: the same as `credentials: {password: ...}`.",
        json_schema_extra={"writeOnly": True},
    )

    def secrets(self) -> dict[str, str] | None:
        """``credentials`` with the deprecated ``password`` folded in."""
        merged = dict(self.credentials or {})
        if self.password is not None:
            merged.setdefault("password", self.password)
        return merged or None


class CredentialChoiceOut(BaseModel):
    """One option of a choice field, its label in the caller's language."""

    value: str
    label: str


class CredentialFieldOut(BaseModel):
    """One input of a provider's link form, its texts in the caller's language."""

    key: str
    kind: CredentialFieldKind
    label: str
    help: str | None = Field(description="Provider-specific hint; generic hints are the app's.")
    placeholder: str | None
    required: bool
    identity: bool
    choices: list[CredentialChoiceOut]
    pattern: str | None
    max_length: int


class ProviderLinkOut(BaseModel):
    """A vendor page that explains where the credentials come from."""

    label: str
    url: str


class ProviderOut(BaseModel):
    """A cloud provider with every text in the caller's language (English fallback)."""

    provider: str
    name: str = Field(description="The vendor's brand, not translated.")
    summary: str | None
    fields: list[CredentialFieldOut]
    setup_steps: list[str]
    links: list[ProviderLinkOut]
    needs_developer_account: bool
    tested_on_hardware: bool

    @classmethod
    def of(cls, item: ProviderDescriptor, locale: str | None) -> "ProviderOut":
        """``item`` resolved for ``locale``."""

        def text(value: Mapping[str, str]) -> str:
            return text_for(value, locale) or ""

        return cls(
            provider=item.provider,
            name=item.name,
            summary=text_for(item.summary, locale),
            fields=[
                CredentialFieldOut(
                    key=f.key,
                    kind=f.kind,
                    label=text(f.label),
                    help=text_for(f.help, locale),
                    placeholder=text_for(f.placeholder, locale),
                    required=f.required,
                    identity=f.identity,
                    choices=[
                        CredentialChoiceOut(value=c.value, label=text(c.label)) for c in f.choices
                    ],
                    pattern=f.pattern,
                    max_length=f.max_length,
                )
                for f in item.fields
            ],
            setup_steps=[text(s) for s in item.setup_steps],
            links=[ProviderLinkOut(label=text(link.label), url=link.url) for link in item.links],
            needs_developer_account=item.needs_developer_account,
            tested_on_hardware=item.tested_on_hardware,
        )


class ProviderList(BaseModel):
    """``GET /linked-accounts/providers``: the cloud providers enabled in the Device-Gateway,
    each with its credential fields and guidance in the caller's language (the web app renders
    the link form from it)."""

    items: list[ProviderOut]


class LinkedAccountOut(BaseModel):
    """A linked account; secret fields are never returned."""

    id: UUID
    provider: str
    identity: str = Field(
        description="The value of the provider's identity field (an e-mail or an application "
        "key): the account's title."
    )
    public_credentials: dict[str, str] = Field(
        default_factory=dict,
        description="The non-secret credential fields as entered (identity included).",
    )
    email: str = Field(description="Deprecated: the same as `identity`.")
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
            identity=item.email,
            public_credentials=CredentialVault.public_of(item),
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
    device_type: DeviceType = Field(
        default=DeviceType.OTHER,
        description="`mower`, `rtk_base` (register it with `POST /base-stations`, ADR 0047) or "
        "`other` (not registrable).",
    )
    registered: bool = Field(
        description="Registered in this account: as a mower, or as a base station."
    )
    mower_id: UUID | None = Field(description="That mower, when `registered` as a mower.")
    base_station_id: UUID | None = Field(
        default=None, description="That base station, when `registered` as a base station."
    )
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
            device_type=device.device_type,
            registered=view.mower_id is not None or view.base_station_id is not None,
            mower_id=view.mower_id,
            base_station_id=view.base_station_id,
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


# --- RTK base stations (ADR 0047) ---------------------------------------------------------------


class BaseStationIn(In):
    """``POST /base-stations``: a device of a linked account (``source`` ``cloud``) or the
    simulator's base station (``simulated``)."""

    source: Literal["cloud", "simulated"] = "cloud"
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Defaults to the device name (`Simulated base station`).",
    )
    linked_account_id: UUID | None = Field(
        default=None, description="`cloud`: the linked account whose device list names it."
    )
    device_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="`cloud`: the device (`device_type` `rtk_base` in the device list).",
    )


class BaseStationPatch(In):
    """``PATCH /base-stations/{id}``."""

    name: str = Field(min_length=1, max_length=100)


class BaseStationOut(BaseModel):
    """An RTK base station. Its live state comes with the linked mowers' telemetry
    (`MowerState.base_station`)."""

    id: UUID
    name: str
    source: Literal["cloud", "simulated"]
    linked_account_id: UUID | None
    device_name: str | None
    iot_id: str | None = Field(description="The vendor's device id (unique across accounts).")
    model: str | None
    mower_ids: list[UUID] = Field(description="The mowers linked to it, by name.")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: BaseStationView) -> "BaseStationOut":
        """From the use case view."""
        base = view.base
        return cls(
            id=base.id,
            name=base.name,
            source="simulated" if base.source == "simulated" else "cloud",
            linked_account_id=base.linked_account_id,
            device_name=base.device_name,
            iot_id=base.iot_id,
            model=base.model,
            mower_ids=list(view.mower_ids),
            created_at=base.created_at,
            updated_at=base.updated_at,
        )


class BaseStationList(BaseModel):
    """``GET /base-stations``."""

    items: list[BaseStationOut]


class BaseStationFirmwareUpdateIn(In):
    """``POST /base-stations/{id}/firmware/update``."""

    version: str = Field(
        min_length=1, max_length=64, description="The offered version (`firmware_info.available`)."
    )
    mower_id: UUID | None = Field(
        default=None,
        description="The linked mower that relays the request; default the first by name.",
    )


class MowerBaseStationIn(In):
    """``PUT /mowers/{id}/base-station``: link the mower to a base station, or unlink it."""

    base_station_id: UUID | None = Field(description="null removes the link.")
