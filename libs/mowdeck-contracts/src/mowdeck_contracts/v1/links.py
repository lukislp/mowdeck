"""Additional links of a mower (ADR 0038): a Bluetooth bridge next to the cloud connection.

A mower has one ``connection_kind`` (how the Device-Gateway reaches it first, often the
manufacturer's cloud) and may have further *links* keyed by kind, today only ``ble_bridge``: a
Bluetooth bridge (an ESPHome Bluetooth proxy, independent of the mower's manufacturer) that sits
next to the mower and carries the low-latency path for manual driving while the primary
connection carries everything else. Which link kinds a mower accepts is declared by its adapter
in ``MowerCapabilities.links_supported``. Fleet stores the links, the Device-Gateway opens them.

* Fleet publishes the public options of every link in
  :attr:`~mowdeck_contracts.v1.fleet.MowerRegistration.links` (``{kind: options}``); like the
  connection options, secrets are withheld and replaced by ``credentials: "fleet"``. The
  Device-Gateway gets them with the connection secrets
  (:attr:`~mowdeck_contracts.v1.fleet.MowerCredentialsReply.link_secrets`).
* The Device-Gateway reports every link in :attr:`~mowdeck_contracts.v1.MowerState.links`
  (``{kind: LinkStatus}``), so clients show whether the bridge is up and how old its firmware is.
* Before a link is stored, Fleet asks the Device-Gateway to look through the proxy for the mower:
  :class:`BridgeScanRequest` on :func:`~mowdeck_contracts.v1.subjects.bridge_scan_subject`
  (core NATS request/reply, internal), answered with a :class:`BridgeScanReply`.

``noise_psk`` is a secret (the bridge's API encryption key, ``api.encryption.key`` of an ESPHome
bridge): excluded from ``repr``, never logged. Nothing here is specific to a mower manufacturer;
the bridge firmware fields describe the bridge, not the mower.
"""

from enum import StrEnum
from typing import Final
from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime

LINK_BLE_BRIDGE: Final = "ble_bridge"
"""Link kind of a Bluetooth bridge (an ESPHome Bluetooth proxy) next to the mower."""
LINK_KINDS: Final = frozenset({LINK_BLE_BRIDGE})
"""All link kinds a registration may carry."""
CREDENTIALS_REV_OPTION: Final = "credentials_rev"
"""Option key of a link: a revision counter Fleet raises whenever the link's secrets change, so
the Device-Gateway knows when to fetch them again (``credentials: "fleet"`` marks them withheld,
see :data:`~mowdeck_contracts.v1.fleet.CREDENTIALS_OPTION`)."""
BRIDGE_SCAN_MAX_TIMEOUT_S: Final = 30
"""Longest a bridge scan may wait for the mower's advertisement."""


class LinkStatus(ContractModel):
    """The state of one additional link as the Device-Gateway reports it in ``MowerState.links``."""

    connected: bool = Field(default=False, description="The link is open right now.")
    since: UtcDateTime | None = Field(
        default=None, description="When the link was opened (or lost), if known."
    )
    rssi: int | None = Field(
        default=None,
        ge=-128,
        le=20,
        description="Signal strength of the mower at the bridge in dBm, if the bridge reports it.",
    )
    esphome_version: str | None = Field(
        default=None,
        max_length=64,
        description="Firmware version of the bridge (the ESPHome release it runs).",
    )
    compiled_at: str | None = Field(
        default=None,
        max_length=64,
        description="When the bridge firmware was compiled, as ESPHome reports it (free text).",
    )
    project_version: str | None = Field(
        default=None,
        max_length=64,
        description="Version of the MowDeck bridge template the firmware was built from; null "
        "when the firmware carries no `esphome.project`.",
    )
    outdated: bool = Field(
        default=False,
        description="The bridge firmware is older than what MowDeck expects; see "
        "`outdated_reason`.",
    )
    outdated_reason: str | None = Field(
        default=None,
        max_length=64,
        description="Why the firmware counts as outdated, e.g. `esphome_version`, "
        "`project_version` or `no_project`; null when up to date.",
    )


class ProxyInfo(ContractModel):
    """What a scan learnt about the bridge itself (ESPHome ``device_info``)."""

    proxy_name: str = Field(max_length=100, description="The ESPHome node name.")
    esphome_version: str | None = Field(default=None, max_length=64)
    compiled_at: str | None = Field(default=None, max_length=64)
    project_name: str | None = Field(
        default=None, max_length=100, description="`esphome.project.name`, if set."
    )
    project_version: str | None = Field(
        default=None, max_length=64, description="`esphome.project.version`, if set."
    )
    bluetooth_mac: str | None = Field(
        default=None, max_length=32, description="The bridge's own Bluetooth address."
    )
    checked_at: UtcDateTime = Field(description="When the gateway read this information.")
    outdated: bool = False
    outdated_reason: str | None = Field(default=None, max_length=64)


class ScannedDevice(ContractModel):
    """One mower advertisement the bridge saw during a scan."""

    name: str = Field(
        min_length=1, max_length=100, description="The Bluetooth name the mower advertises."
    )
    address: str = Field(min_length=1, max_length=32, description="Bluetooth address.")
    rssi: int = Field(ge=-128, le=20, description="Signal strength at the bridge in dBm.")


class BridgeScanRequest(ContractModel):
    """Request on ``gateway.mower.<id>.bridge.scan`` (internal, Fleet to Device-Gateway).

    The gateway connects to the proxy at ``host``:``port`` with ``noise_psk``, reads its device
    information and collects mower advertisements until ``expected_name`` shows up or
    ``timeout_s`` passes. While the gateway already holds an open link through this proxy it
    answers from the live scanner instead (a re-check).
    """

    mower_id: UUID
    account_id: UUID = Field(description="The mower's account as Fleet knows it.")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=6053, ge=1, le=65535)
    noise_psk: str = Field(
        repr=False,
        min_length=1,
        max_length=256,
        description="The bridge's API encryption key (`api.encryption.key` of an ESPHome bridge).",
    )
    expected_name: str | None = Field(
        default=None,
        max_length=100,
        description="The mower's Bluetooth name (its cloud device name); the scan ends early "
        "when it is seen.",
    )
    timeout_s: int = Field(default=15, ge=1, le=BRIDGE_SCAN_MAX_TIMEOUT_S)


class BridgeScanErrorCode(StrEnum):
    """Why a bridge scan gave no result."""

    UNREACHABLE = "unreachable"
    """No connection to the proxy at host and port."""
    AUTH_FAILED = "auth_failed"
    """The proxy refused the encryption key."""
    TIMEOUT = "timeout"
    """The proxy answered but the expected mower did not advertise within the timeout."""
    BUSY = "busy"
    """The proxy is in use by another scan or link right now; try again shortly."""
    UNSUPPORTED = "unsupported"
    """The gateway cannot scan through this kind of bridge."""
    NOT_FOUND = "not_found"
    """The gateway does not hold the mower (or the shard owner is down)."""
    FORBIDDEN = "forbidden"
    """The account of the request does not own the mower."""
    ALREADY_CONNECTED = "already_connected"
    """The expected mower was not seen, and the gateway itself holds a Bluetooth connection
    through this bridge for another mower entry (a connected mower stops advertising);
    ``held_by`` names that entry."""


class BridgeScanError(ContractModel):
    """The error of a :class:`BridgeScanReply`; ``detail`` never echoes the key."""

    code: BridgeScanErrorCode
    detail: str = Field(max_length=500)
    held_by: str | None = Field(
        default=None,
        max_length=100,
        description="For already_connected: the name of the mower entry whose connection holds "
        "the mower through this bridge.",
    )


class BridgeScanReply(ContractModel):
    """Reply to :class:`BridgeScanRequest`.

    ``proxy`` is set as soon as the proxy answered, also with a ``timeout`` error (the bridge
    works, the mower was not seen); ``devices`` lists every mower advertisement seen, so the
    client can offer a different mower than expected. ``error`` is null when the scan succeeded.
    """

    proxy: ProxyInfo | None = None
    devices: list[ScannedDevice] = Field(default_factory=list, max_length=50)
    error: BridgeScanError | None = None

    @property
    def ok(self) -> bool:
        """True when the scan completed without an error."""
        return self.error is None
