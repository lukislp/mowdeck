"""Mower registrations shared by Fleet and the Device-Gateway.

Fleet owns the mowers; the Device-Gateway keeps a volatile copy of the registrations it needs
to connect. The copy is filled in two ways:

* a core NATS request on :data:`~mowdeck_contracts.v1.subjects.FLEET_MOWERS_LIST` with a
  :class:`MowerListRequest` (the gateway's shard), answered with a :class:`MowerListReply`;
* the events ``mower.registered``, ``mower.updated`` and ``mower.removed`` with a
  :class:`MowerRegistration` payload inside an
  :class:`~mowdeck_contracts.v1.EventEnvelope`.

The Device-Gateway reports links with ``mower.connected`` and ``mower.disconnected`` events
carrying a :class:`MowerConnectionEvent`.

Secrets never travel in a registration (ADR 0034): Fleet withholds passwords, ESPHome keys and
the vendor account from ``connection_options`` and sets ``credentials: "fleet"`` instead; the
Device-Gateway asks for them on
:data:`~mowdeck_contracts.v1.subjects.FLEET_MOWERS_CREDENTIALS` (:class:`MowerCredentialsRequest`,
:class:`MowerCredentialsReply`) each time it opens the connection and keeps them only in that
connection.
"""

from typing import Any, Final, Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime, utc_now
from mowdeck_contracts.v1.mower import ConnectionKind

MOWER_REGISTERED: Final = "mower.registered"
"""Event type: a mower was registered in Fleet (payload :class:`MowerRegistration`)."""
MOWER_UPDATED: Final = "mower.updated"
"""Event type: name, account or connection of a mower changed (payload
:class:`MowerRegistration`)."""
MOWER_REMOVED: Final = "mower.removed"
"""Event type: a mower was removed from Fleet (payload :class:`MowerRegistration`)."""
MOWER_CONNECTED: Final = "mower.connected"
"""Event type: the Device-Gateway connected to a mower (payload :class:`MowerConnectionEvent`)."""
MOWER_DISCONNECTED: Final = "mower.disconnected"
"""Event type: the Device-Gateway lost or closed the link (payload
:class:`MowerConnectionEvent`)."""

CREDENTIALS_OPTION: Final = "credentials"
"""Key in ``connection_options`` that marks withheld secrets; its value is
:data:`CREDENTIALS_FROM_FLEET`."""
CREDENTIALS_FROM_FLEET: Final = "fleet"
"""The Device-Gateway fetches the secrets of this mower with :class:`MowerCredentialsRequest`."""
SECRET_OPTION_KEYS: Final = frozenset({"account", "password", "noise_psk"})
"""Option keys Fleet never puts into a registration (the vendor account counts: it is the login
name of the password)."""
_MASK = "***"


class MowerRegistration(ContractModel):
    """What the Device-Gateway needs to know to connect to one mower.

    ``connection_options`` are adapter specific (for the simulator: ``SimulationConfig``
    fields such as ``time_scale``). They never carry secrets (ADR 0034): Fleet withholds
    ``password``, ``noise_psk`` and the vendor ``account`` and marks the registration with
    ``credentials: "fleet"`` (:attr:`needs_credentials`); the Device-Gateway fetches them with
    :class:`MowerCredentialsRequest` when it connects. References (``credential_ref``) stay in
    the options and are resolved by the gateway. ``repr`` masks secret keys anyway.
    """

    mower_id: UUID
    account_id: UUID = Field(description="Owning account; commands of other accounts are refused.")
    name: str = Field(min_length=1, max_length=100)
    connection_kind: ConnectionKind
    connection_options: dict[str, Any] = Field(default_factory=dict)

    def __repr_args__(self) -> Any:
        """``repr`` masks the values of secret option keys (defence in depth for logs)."""
        for key, value in super().__repr_args__():
            if key == "connection_options" and isinstance(value, dict):
                yield (
                    key,
                    {
                        k: _MASK if k in SECRET_OPTION_KEYS or "secret" in k else v
                        for k, v in value.items()
                    },
                )
            else:
                yield key, value

    @property
    def needs_credentials(self) -> bool:
        """True when Fleet withheld secrets the gateway must fetch before connecting."""
        return self.connection_options.get(CREDENTIALS_OPTION) == CREDENTIALS_FROM_FLEET


class MowerCredentialsRequest(ContractModel):
    """Request on ``fleet.mowers.credentials`` (internal, Device-Gateway to Fleet)."""

    mower_id: UUID
    account_id: UUID = Field(description="The mower's account as the gateway knows it.")


class MowerCredentialsReply(ContractModel):
    """Reply to :class:`MowerCredentialsRequest`: the withheld options (``account`` and
    ``password`` of a cloud mower, ``noise_psk`` of a BLE bridge), merged by the gateway into
    the options of the connection it opens. ``found`` is False for an unknown mower or another
    account. Never log ``secret_options``."""

    found: bool
    secret_options: dict[str, str] = Field(default_factory=dict, repr=False)


class MowerListRequest(ContractModel):
    """Request on ``fleet.mowers.list``: the registrations of one Device-Gateway shard.

    A mower belongs to shard ``i`` of ``n`` when ``mower_id.int % n == i``. The defaults ask
    for every mower.
    """

    shard_index: int = Field(default=0, ge=0)
    shard_count: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _index_in_range(self) -> Self:
        if self.shard_index >= self.shard_count:
            msg = "shard_index must be smaller than shard_count"
            raise ValueError(msg)
        return self


class MowerListReply(ContractModel):
    """Reply to :class:`MowerListRequest`."""

    mowers: list[MowerRegistration] = Field(default_factory=list)


class MowerConnectionEvent(ContractModel):
    """Payload of ``mower.connected`` and ``mower.disconnected``."""

    mower_id: UUID
    account_id: UUID
    connection_kind: ConnectionKind
    at: UtcDateTime = Field(default_factory=utc_now)
    reason: str | None = Field(
        default=None, max_length=500, description="Why the link ended (disconnected only)."
    )
