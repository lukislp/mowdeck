"""Events published by the Identity service.

All are wrapped in :class:`~mowdeck_contracts.v1.EventEnvelope` and published on
``event.identity.<name>`` (stream ``EVENTS``):

==============================  ======================  ===========================================
Event type                      Payload                 Meaning
==============================  ======================  ===========================================
``identity.account.created``    :class:`AccountCreated`  a new account with its owner exists
``identity.user.created``       :class:`UserChanged`     a user was created (registration, sub-user)
``identity.user.disabled``      :class:`UserChanged`     a user or a membership was disabled
``identity.user.deleted``       :class:`UserChanged`     a sub-user was deleted or a member removed
``identity.grants.changed``     :class:`GrantsChanged`   authorization tuples of subjects changed
``identity.api_token.revoked``  :class:`ApiTokenRevoked`  an API token was revoked
``identity.session.new_device``  :class:`NewDeviceLogin`  a login came from a device the user had
                                                        not used before (ADR 0065)
==============================  ======================  ===========================================

``identity.grants.changed`` is the cache invalidation signal for ``mowdeck-authz``: every
service that caches authorization decisions drops the entries of the named subjects
(``mowdeck_authz.events.subscribe_cache_invalidation``).
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final
from uuid import UUID

from pydantic import Field, StringConstraints

from mowdeck_contracts.v1._base import ContractModel

IDENTITY_ACCOUNT_CREATED: Final = "identity.account.created"
"""Event type: an account was registered (payload :class:`AccountCreated`)."""
IDENTITY_USER_CREATED: Final = "identity.user.created"
"""Event type: a user was created (payload :class:`UserChanged`)."""
IDENTITY_USER_DISABLED: Final = "identity.user.disabled"
"""Event type: a user or a membership was disabled (payload :class:`UserChanged`)."""
IDENTITY_USER_DELETED: Final = "identity.user.deleted"
"""Event type: a user was deleted or removed from an account (payload :class:`UserChanged`)."""
IDENTITY_GRANTS_CHANGED: Final = "identity.grants.changed"
"""Event type: authorization tuples of subjects changed (payload :class:`GrantsChanged`)."""
IDENTITY_API_TOKEN_REVOKED: Final = "identity.api_token.revoked"  # noqa: S105 - event type
"""Event type: an API token was revoked (payload :class:`ApiTokenRevoked`)."""

IDENTITY_SESSION_NEW_DEVICE: Final = "identity.session.new_device"
"""Event type: a login from a device the user never used before (payload :class:`NewDeviceLogin`,
ADR 0065); Notifications tells the user."""

IDENTITY_EVENT_TYPES: Final = (
    IDENTITY_ACCOUNT_CREATED,
    IDENTITY_USER_CREATED,
    IDENTITY_USER_DISABLED,
    IDENTITY_USER_DELETED,
    IDENTITY_GRANTS_CHANGED,
    IDENTITY_API_TOKEN_REVOKED,
    IDENTITY_SESSION_NEW_DEVICE,
)
"""Every event type Identity publishes."""

FgaSubject = Annotated[
    str, StringConstraints(pattern=r"^(user|api_token):[0-9a-fA-F-]{36}$", max_length=64)
]
"""An OpenFGA subject of Identity: ``user:<uuid>`` or ``api_token:<uuid>``."""


class AccountKind(StrEnum):
    """What an account is."""

    HOUSEHOLD = "household"
    BUSINESS = "business"


class AccountCreated(ContractModel):
    """Payload of ``identity.account.created``.

    Fleet creates the hidden default site of the account when it receives this event.
    """

    account_id: UUID
    name: str = Field(min_length=1, max_length=200)
    kind: AccountKind
    owner_user_id: UUID


class UserChanged(ContractModel):
    """Payload of ``identity.user.created``, ``identity.user.disabled`` and ``.deleted``.

    ``account_id`` is the account in which the change happened (None only for platform level
    changes). ``subuser`` is True for users owned by the account.
    """

    user_id: UUID
    account_id: UUID | None = None
    username: str = Field(min_length=1, max_length=64)
    subuser: bool = False
    reason: str | None = Field(default=None, max_length=200)


class GrantsChanged(ContractModel):
    """Payload of ``identity.grants.changed``: drop cached decisions of these subjects.

    ``all_subjects`` asks consumers to drop their whole cache (for example after a reconcile
    that touched many subjects).
    """

    account_id: UUID | None = None
    subjects: list[FgaSubject] = Field(default_factory=list, max_length=1000)
    all_subjects: bool = False


class ApiTokenRevoked(ContractModel):
    """Payload of ``identity.api_token.revoked``."""

    token_id: UUID
    account_id: UUID
    reason: str = Field(min_length=1, max_length=200)


class NewDeviceLogin(ContractModel):
    """Payload of ``identity.session.new_device`` (ADR 0065).

    Published only when the user has new-login alerts on and already had a known device (the
    very first login, and the first one after the feature arrived, record the device silently).
    Nothing in it is a secret: the device is described by its user agent, the address only by
    its network (``203.0.113.x``).
    """

    user_id: UUID
    account_id: UUID
    """The account the session was started in (the user's notification preferences of it)."""
    session_id: UUID
    """The login session (``sid``): the security page can end exactly this one."""
    device: str = Field(min_length=1, max_length=100)
    """``Firefox on Windows``, ``MowDeck app on iOS``."""
    client: str = Field(default="unknown", pattern=r"^(browser|app|script|unknown)$")
    browser: str | None = Field(default=None, max_length=40)
    """Browser family without version (``Firefox``); None for the app and scripts."""
    os: str | None = Field(default=None, max_length=40)
    """Operating system (``Windows``, ``iOS``), when the user agent tells."""
    ip_prefix: str | None = Field(default=None, max_length=64)
    network: str = Field(default="unknown", pattern=r"^(local|public|unknown)$")
    method: str = Field(min_length=1, max_length=20)
    """How the user signed in: ``password``, ``passkey``, ``github``, ``apple``."""
    at: datetime
