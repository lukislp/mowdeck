"""Entities of Identity. Plain mutable dataclasses; repositories store and load them.

Field names equal the column names of the database tables, so the adapters map them 1:1.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class AccountStatus(StrEnum):
    """Lifecycle of an account."""

    ACTIVE = "active"
    DELETING = "deleting"
    """Deletion requested: locked for everyone but the owner until ``delete_after``."""


class UserKind(StrEnum):
    """A person registers; a sub-user is created and owned by an account."""

    PERSON = "person"
    SUBUSER = "subuser"


class Status(StrEnum):
    """Status of a user or a membership."""

    ACTIVE = "active"
    DISABLED = "disabled"


class RoleKind(StrEnum):
    """System roles mirror fixed relations; default and custom roles are permission bundles."""

    SYSTEM_OWNER = "system_owner"
    SYSTEM_ADMIN = "system_admin"
    DEFAULT = "default"
    CUSTOM = "custom"


class SubjectType(StrEnum):
    """Who can hold rights."""

    USER = "user"
    API_TOKEN = "api_token"  # noqa: S105 - subject type, not a secret


class LevelType(StrEnum):
    """Where a right is granted; it applies to everything below."""

    ACCOUNT = "account"
    SITE = "site"
    GROUP = "group"
    MOWER = "mower"


class KeyStatus(StrEnum):
    """Lifecycle of a signing key."""

    NEXT = "next"
    """Published in the JWKS, not used for signing yet."""
    ACTIVE = "active"
    """Signs new tokens."""
    RETIRING = "retiring"
    """Published until the tokens it signed have expired."""
    RETIRED = "retired"
    """Not published; tokens signed with it fail."""


@dataclass(frozen=True, slots=True)
class Level:
    """A level of the hierarchy: ``account:<id>``, ``site:<id>``, ``group:<id>``, ``mower:<id>``."""

    type: LevelType
    id: UUID

    def __str__(self) -> str:
        return f"{self.type.value}:{self.id}"


@dataclass(slots=True)
class Account:
    """A household or business: the data and billing boundary."""

    id: UUID
    name: str
    slug: str
    kind: str
    status: AccountStatus
    plan: str
    created_at: datetime
    delete_after: datetime | None = None

    def __post_init__(self) -> None:
        self.status = AccountStatus(self.status)


@dataclass(slots=True)
class User:
    """A person or a sub-user. Usernames are unique per installation."""

    id: UUID
    username: str
    username_norm: str
    display_name: str
    kind: UserKind
    status: Status
    created_at: datetime
    password_hash: str | None = field(default=None, repr=False)
    email: str | None = None
    email_norm: str | None = None
    owner_account_id: UUID | None = None
    """Set for sub-users: the account that owns them."""
    created_by: UUID | None = None
    must_change_password: bool = False
    last_login_at: datetime | None = None

    def __post_init__(self) -> None:
        self.kind = UserKind(self.kind)
        self.status = Status(self.status)


@dataclass(slots=True)
class Membership:
    """A user's place in an account. Owner and admin are fixed relations, not roles."""

    id: UUID
    account_id: UUID
    user_id: UUID
    status: Status
    created_at: datetime
    is_owner: bool = False
    is_admin: bool = False

    def __post_init__(self) -> None:
        self.status = Status(self.status)


@dataclass(slots=True)
class Role:
    """A named bundle of permission keys of one account."""

    id: UUID
    account_id: UUID
    key: str
    name: str
    kind: RoleKind
    permissions: list[str]
    created_at: datetime
    updated_at: datetime
    description: str = ""

    @property
    def is_system(self) -> bool:
        """Owner and Admin: not editable, mapped to fixed relations."""
        return self.kind in (RoleKind.SYSTEM_OWNER, RoleKind.SYSTEM_ADMIN)

    def __post_init__(self) -> None:
        self.kind = RoleKind(self.kind)
        self.permissions = list(self.permissions)


@dataclass(slots=True)
class RoleAssignment:
    """A role held by a subject at a level of an account."""

    id: UUID
    account_id: UUID
    subject_type: SubjectType
    subject_id: UUID
    role_id: UUID
    level_type: LevelType
    level_id: UUID
    created_at: datetime
    created_by: UUID | None = None

    @property
    def level(self) -> Level:
        """The level as a value object."""
        return Level(self.level_type, self.level_id)

    def __post_init__(self) -> None:
        self.subject_type = SubjectType(self.subject_type)
        self.level_type = LevelType(self.level_type)


@dataclass(slots=True)
class Session:
    """One refresh token of a login session (``family_id``); rotated on every refresh."""

    id: UUID
    family_id: UUID
    user_id: UUID
    account_id: UUID
    refresh_hash: str = field(repr=False)
    created_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    last_used_at: datetime | None = None
    replaced_by: UUID | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None
    user_agent: str | None = None
    ip: str | None = None


@dataclass(frozen=True, slots=True)
class TokenGrant:
    """Permission keys of an API token at one level."""

    level: Level
    permissions: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        """Storage form."""
        return {
            "level_type": self.level.type.value,
            "level_id": str(self.level.id),
            "permissions": list(self.permissions),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TokenGrant":
        """Inverse of :meth:`to_json`."""
        return cls(
            Level(LevelType(data["level_type"]), UUID(str(data["level_id"]))),
            tuple(data["permissions"]),
        )


@dataclass(slots=True)
class ApiToken:
    """A long-lived credential (Home Assistant), exchanged for short access tokens."""

    id: UUID
    account_id: UUID
    name: str
    prefix: str
    secret_hash: str = field(repr=False)
    created_by: UUID
    grants: list[dict[str, Any]]
    """Stored :class:`TokenGrant` dicts."""
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None

    @property
    def token_grants(self) -> list[TokenGrant]:
        """The grants as value objects."""
        return [TokenGrant.from_json(g) for g in self.grants]

    @property
    def permissions(self) -> list[str]:
        """Union of keys over all grants, sorted."""
        return sorted({key for grant in self.token_grants for key in grant.permissions})

    def is_usable(self, now: datetime) -> bool:
        """Not revoked and not expired."""
        return self.revoked_at is None and (self.expires_at is None or now < self.expires_at)


@dataclass(slots=True)
class SigningKeyRecord:
    """A signing key at rest: public JWK in clear, private key encrypted with the KEK."""

    kid: str
    alg: str
    public_jwk: dict[str, Any]
    private_key_enc: bytes = field(repr=False)
    status: KeyStatus
    created_at: datetime
    activated_at: datetime | None = None
    retire_after: datetime | None = None

    def __post_init__(self) -> None:
        self.status = KeyStatus(self.status)
        self.private_key_enc = bytes(self.private_key_enc)


@dataclass(slots=True)
class AuditEntry:
    """One append-only audit record. ``details`` never contains secrets."""

    at: datetime
    action: str
    actor_subject: str | None
    actor_kind: str | None
    outcome: str = "success"
    account_id: UUID | None = None
    actor_name: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    ip: str | None = None
    correlation_id: str | None = None
    id: int | None = None


@dataclass(slots=True)
class LoginAttempt:
    """A login attempt, for throttling by username."""

    id: UUID
    at: datetime
    username_norm: str
    success: bool
    ip: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class CatalogueEntry:
    """A permission key known to the installation (from module descriptors and the model)."""

    key: str
    description: str
    module: str
    levels: list[str]
    """Levels where the key may be granted (only ``account`` for keys outside the model)."""
    in_model: bool
    """True if the authorization model has a relation for it (checked per object in OpenFGA)."""
    first_seen_at: datetime
    last_seen_at: datetime
    implied_by: list[str] = field(default_factory=list)
    users_only: bool = False
    """True for administrative keys API tokens may never hold."""
    retired: bool = False
