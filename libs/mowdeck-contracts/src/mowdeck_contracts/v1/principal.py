"""Who is acting: the authenticated principal.

The fields mirror the claims of the short-lived access tokens issued by the Identity service
(JWT signed with EdDSA, lifetime 5 to 15 minutes). Every service verifies the token itself with
the published public key and turns the claims into a :class:`Principal`; there is no call back to
Identity per request.
"""

from enum import StrEnum
from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime


class PrincipalKind(StrEnum):
    """What kind of subject a principal is."""

    USER = "user"
    """An account owner or a person that is a member of an account."""
    SUBUSER = "subuser"
    """A user created and owned by an account (for example staff without e-mail)."""
    API_TOKEN = "api_token"  # noqa: S105 - enum label, not a secret
    """A long-lived API token, for example for Home Assistant, limited to chosen permissions."""
    SERVICE = "service"
    """Another MowDeck service acting on its own behalf."""


class PrincipalRef(ContractModel):
    """A compact reference to a principal, embedded in commands and audit records."""

    subject: UUID = Field(description="Stable subject id (JWT `sub`).")
    kind: PrincipalKind = Field(description="Kind of subject.")
    display_name: str | None = Field(default=None, max_length=200)
    token_id: UUID | None = Field(default=None, description="Token id (JWT `jti`), if known.")


class Principal(ContractModel):
    """The verified identity behind a request, derived from the access token claims.

    ``account_id`` is the data boundary. Services must take the account id only from a verified
    principal, never from a request body or query string.
    """

    subject: UUID = Field(description="Stable subject id (JWT `sub`).")
    account_id: UUID | None = Field(
        default=None,
        description="Account (household or business) the token is scoped to (JWT `acc`). "
        "None for operator or service principals that are not bound to one account.",
    )
    kind: PrincipalKind
    display_name: str | None = Field(default=None, max_length=200)
    roles: list[str] = Field(default_factory=list, description="Role names granted in the account.")
    permissions: list[str] = Field(
        default_factory=list,
        description="Effective permission keys such as `mower.control` (coarse, token level). "
        "Object-level checks are answered by the authorization service.",
    )
    plan: str | None = Field(default=None, description="Subscription plan key of the account.")
    token_id: UUID | None = Field(default=None, description="Token id (JWT `jti`).")
    session_id: UUID | None = Field(
        default=None,
        description="Login session (JWT `sid`); None for API tokens and service principals.",
    )
    auth_time: UtcDateTime | None = Field(
        default=None,
        description="When the user last authenticated interactively (JWT `auth_time`, OIDC): "
        "a login or a step-up confirmation. None for API tokens, services and old sessions.",
    )
    auth_methods: list[str] = Field(
        default_factory=list,
        description="How the user authenticated at `auth_time` (JWT `amr`): `pwd`, `passkey`, "
        "`github`, `apple`.",
    )

    def has_permission(self, permission: str) -> bool:
        """Return True if ``permission`` is among the token level permissions."""
        return permission in self.permissions

    def ref(self) -> PrincipalRef:
        """Return a compact :class:`PrincipalRef` for embedding in commands and audit logs."""
        return PrincipalRef(
            subject=self.subject,
            kind=self.kind,
            display_name=self.display_name,
            token_id=self.token_id,
        )
