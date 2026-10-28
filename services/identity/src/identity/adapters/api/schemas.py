"""Request and response bodies of the Identity API."""

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from mowdeck_contracts.v1 import AccountKind, UiAreaDef
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from identity.application.sessions import MeView, TokenPair
from identity.application.users import MemberView
from identity.domain.models import (
    Account,
    ApiToken,
    AuditEntry,
    CatalogueEntry,
    ExternalIdentity,
    Level,
    LevelType,
    Passkey,
    Role,
    RoleAssignment,
    TokenPurpose,
)

Password = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Username = Annotated[str, StringConstraints(min_length=1, max_length=64)]
PermissionKey = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=128)
]


class In(BaseModel):
    """Base of request bodies: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class RegisterIn(In):
    """``POST /register``."""

    username: Username
    password: Password
    account_name: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    account_kind: AccountKind = AccountKind.HOUSEHOLD


class RegisteredOut(BaseModel):
    """The new account and its owner."""

    account_id: UUID
    user_id: UUID


class LoginIn(In):
    """``POST /session``."""

    username: Username
    password: Password
    account_id: UUID | None = Field(default=None, description="Account to act for (default: own)")


class RefreshIn(In):
    """``POST /session/refresh`` and ``/session/logout``; the cookie may be used instead."""

    refresh_token: str | None = Field(default=None, max_length=200)


class TokenExchangeIn(In):
    """``POST /token``: exchange an API token for an access token."""

    grant_type: Literal["api_token"] = "api_token"
    token: str = Field(min_length=1, max_length=200)


class TokenOut(BaseModel):
    """An access token (and for logins the refresh token, shown once)."""

    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - token type, not a secret
    expires_in: int
    expires_at: datetime
    account_id: UUID | None
    session_id: UUID | None = None
    auth_time: datetime | None = Field(
        default=None,
        description="When the user last authenticated interactively (login or step-up); critical "
        "actions need this to be recent",
    )
    refresh_token: str | None = None
    refresh_expires_at: datetime | None = None

    @classmethod
    def of(cls, pair: TokenPair) -> "TokenOut":
        """From a :class:`TokenPair`."""
        return cls(
            access_token=pair.access.token,
            expires_in=pair.access.expires_in,
            expires_at=pair.access.expires_at,
            account_id=pair.principal.account_id,
            session_id=pair.principal.session_id,
            auth_time=pair.principal.auth_time,
            refresh_token=pair.refresh_token,
            refresh_expires_at=pair.refresh_expires_at,
        )


class PasswordChangeIn(In):
    """``PUT /me/password``."""

    current_password: Password
    new_password: Password


class PermissionQuery(In):
    """One question of ``POST /me/permissions``."""

    relation: Literal[
        "can_view",
        "can_control",
        "can_drive",
        "can_edit_schedule",
        "can_bulk_command",
        "can_manage_mowers",
        "can_manage_users",
        "can_manage_billing",
        "can_view_audit",
        "can_manage_account",
        "can_emergency_stop",
        "member",
    ]
    object: Annotated[
        str, StringConstraints(pattern=r"^(account|site|group|mower):[0-9a-fA-F-]{36}$")
    ]


class PermissionsIn(In):
    """``POST /me/permissions``: up to 100 questions."""

    checks: list[PermissionQuery] = Field(max_length=100)


class MembershipOut(BaseModel):
    """A membership in ``GET /me``."""

    account_id: UUID
    account_name: str
    account_status: str
    is_owner: bool
    is_admin: bool
    status: str


class MeOut(BaseModel):
    """``GET /me``."""

    subject: UUID
    kind: str
    account_id: UUID | None
    display_name: str | None
    username: str | None = None
    email: str | None = None
    must_change_password: bool = False
    roles: list[str]
    permissions: list[str]
    plan: str | None
    session_id: UUID | None
    memberships: list[MembershipOut] = Field(default_factory=list)

    @classmethod
    def of(cls, view: MeView) -> "MeOut":
        """From a :class:`MeView`."""
        p = view.principal
        return cls(
            subject=p.subject,
            kind=p.kind.value,
            account_id=p.account_id,
            display_name=p.display_name,
            username=view.user.username if view.user else None,
            email=view.user.email if view.user else None,
            must_change_password=view.user.must_change_password if view.user else False,
            roles=p.roles,
            permissions=p.permissions,
            plan=p.plan,
            session_id=p.session_id,
            memberships=[
                MembershipOut(
                    account_id=m.account_id,
                    account_name=m.account_name,
                    account_status=m.account_status,
                    is_owner=m.is_owner,
                    is_admin=m.is_admin,
                    status=m.status,
                )
                for m in view.memberships
            ],
        )


class UiAreaOut(BaseModel):
    """``GET /ui-areas``: one area of the web app shell and the module that announces it."""

    module: str
    module_version: str
    route: str
    menu_label: str
    labels: dict[str, str] = Field(default_factory=dict)
    required_permission: str

    @classmethod
    def of(cls, module: str, version: str, area: UiAreaDef) -> "UiAreaOut":
        """From a descriptor's :class:`UiAreaDef`."""
        return cls(
            module=module,
            module_version=version,
            route=area.route,
            menu_label=area.menu_label,
            labels=dict(area.labels),
            required_permission=area.required_permission,
        )


class AccountOut(BaseModel):
    """An account."""

    id: UUID
    name: str
    slug: str
    kind: str
    status: str
    plan: str
    created_at: datetime
    delete_after: datetime | None

    @classmethod
    def of(cls, acct: Account) -> "AccountOut":
        """From the entity."""
        return cls(
            id=acct.id,
            name=acct.name,
            slug=acct.slug,
            kind=acct.kind,
            status=acct.status.value,
            plan=acct.plan,
            created_at=acct.created_at,
            delete_after=acct.delete_after,
        )


class AccountPatch(In):
    """``PATCH /accounts/{id}``."""

    name: str = Field(min_length=1, max_length=200)


class AccountDeleteIn(In):
    """``DELETE /accounts/{id}``: the owner's password, or none after a step-up confirmation."""

    password: Password | None = None


class LevelIn(In):
    """A level: ``{"type": "group", "id": "<uuid>"}``."""

    type: LevelType
    id: UUID

    def level(self) -> Level:
        """As a value object."""
        return Level(self.type, self.id)


class LevelOut(BaseModel):
    """A level."""

    type: str
    id: UUID


class AssignmentIn(In):
    """A role at a level (inside ``POST /users``)."""

    role_id: UUID
    level: LevelIn


class RoleAssignmentIn(AssignmentIn):
    """``POST /role-assignments``."""

    subject_type: Literal["user"] = "user"
    subject_id: UUID


class RoleAssignmentOut(BaseModel):
    """An assignment."""

    id: UUID
    subject_type: str
    subject_id: UUID
    role_id: UUID
    level: LevelOut
    created_at: datetime
    created_by: UUID | None

    @classmethod
    def of(cls, a: RoleAssignment) -> "RoleAssignmentOut":
        """From the entity."""
        return cls(
            id=a.id,
            subject_type=a.subject_type.value,
            subject_id=a.subject_id,
            role_id=a.role_id,
            level=LevelOut(type=a.level_type.value, id=a.level_id),
            created_at=a.created_at,
            created_by=a.created_by,
        )


class SubUserIn(In):
    """``POST /users``: a sub-user with username and password (no e-mail needed)."""

    username: Username
    password: Password
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    must_change_password: bool = False
    assignments: list[AssignmentIn] = Field(default_factory=list, max_length=50)


class UserPatch(In):
    """``PATCH /users/{id}``."""

    display_name: str = Field(min_length=1, max_length=200)


class PasswordSetIn(In):
    """``POST /users/{id}/password``: a temporary password for a sub-user."""

    password: Password


class MemberOut(BaseModel):
    """A member of the account."""

    id: UUID
    username: str
    display_name: str
    email: str | None
    kind: str
    subuser: bool
    status: str
    membership_status: str
    is_owner: bool
    is_admin: bool
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None
    assignments: list[RoleAssignmentOut]

    @classmethod
    def of(cls, view: MemberView) -> "MemberOut":
        """From a :class:`MemberView`."""
        u, m = view.user, view.membership
        return cls(
            id=u.id,
            username=u.username,
            display_name=u.display_name,
            email=u.email,
            kind=u.kind.value,
            subuser=view.subuser,
            status=u.status.value,
            membership_status=m.status.value,
            is_owner=m.is_owner,
            is_admin=m.is_admin,
            must_change_password=u.must_change_password,
            created_at=u.created_at,
            last_login_at=u.last_login_at,
            assignments=[RoleAssignmentOut.of(a) for a in view.assignments],
        )


class RoleIn(In):
    """``POST /roles``."""

    name: str = Field(min_length=1, max_length=100)
    permissions: list[PermissionKey] = Field(max_length=100)
    description: str = Field(default="", max_length=500)


class RolePatch(In):
    """``PATCH /roles/{id}``."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    permissions: list[PermissionKey] | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class RoleOut(BaseModel):
    """A role."""

    id: UUID
    key: str
    name: str
    kind: str
    permissions: list[str]
    description: str
    editable: bool

    @classmethod
    def of(cls, role: Role) -> "RoleOut":
        """From the entity."""
        return cls(
            id=role.id,
            key=role.key,
            name=role.name,
            kind=role.kind.value,
            permissions=role.permissions,
            description=role.description,
            editable=not role.is_system,
        )


class PermissionOut(BaseModel):
    """A catalogue entry."""

    key: str
    description: str
    module: str
    levels: list[str]
    in_model: bool
    api_tokens: bool
    implied_by: list[str]

    @classmethod
    def of(cls, entry: CatalogueEntry) -> "PermissionOut":
        """From the entity."""
        return cls(
            key=entry.key,
            description=entry.description,
            module=entry.module,
            levels=entry.levels,
            in_model=entry.in_model,
            api_tokens=not entry.users_only,
            implied_by=entry.implied_by,
        )


class TokenGrantIn(In):
    """Permission keys of an API token at one level."""

    level: LevelIn
    permissions: list[PermissionKey] = Field(min_length=1, max_length=50)


class ApiTokenIn(In):
    """``POST /api-tokens``."""

    name: str = Field(min_length=1, max_length=100)
    grants: list[TokenGrantIn] = Field(min_length=1, max_length=50)
    expires_at: datetime | None = None


class ApiTokenOut(BaseModel):
    """An API token (never with its secret, except once at creation)."""

    id: UUID
    name: str
    prefix: str
    created_by: UUID
    grants: list[dict[str, Any]]
    permissions: list[str]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    purpose: TokenPurpose = Field(
        description="`integration` (created by an admin) or `personal` (a user's own personal "
        "access token, ADR 0064)"
    )
    token: str | None = Field(default=None, description="The secret, returned only at creation")

    @classmethod
    def of(cls, t: ApiToken, secret: str | None = None) -> "ApiTokenOut":
        """From the entity."""
        return cls(
            id=t.id,
            name=t.name,
            prefix=t.prefix,
            created_by=t.created_by,
            grants=t.grants,
            permissions=t.permissions,
            created_at=t.created_at,
            expires_at=t.expires_at,
            last_used_at=t.last_used_at,
            revoked_at=t.revoked_at,
            purpose=t.purpose,
            token=secret,
        )


class PersonalTokenIn(In):
    """``POST /me/api-tokens``: a personal access token for the caller's own scripts."""

    name: str = Field(min_length=1, max_length=100, examples=["Garden dashboard script"])
    scopes: list[PermissionKey] = Field(
        min_length=1,
        max_length=3,
        examples=[["mower.view", "mower.control"]],
        description="From `mower.view` (read state, telemetry, history, realtime), "
        "`mower.control` (start, pause, resume, dock) and `schedule.edit`; `mower.view` is "
        "required with any other. Each must be held by the caller on `level`.",
    )
    expires_in_days: int = Field(
        ge=1, le=365, examples=[90], description="The token stops working after this many days."
    )
    level: LevelIn | None = Field(
        default=None,
        description="Where the scopes apply; default the whole account (a site, group or "
        "mower of the account limits the token to it).",
    )


class PersonalTokenOut(BaseModel):
    """A personal access token (never with its secret, except once at creation)."""

    id: UUID
    name: str
    prefix: str = Field(description="The first characters of the secret, to recognise it")
    scopes: list[str]
    level: LevelOut
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = Field(
        description="Last exchange for an access token (updated at most every few minutes)"
    )
    revoked_at: datetime | None
    token: str | None = Field(
        default=None,
        description="The secret (`mdk_...`), returned only at creation. Exchange it at "
        "`POST /api/v1/identity/token` for 10 minute access tokens.",
    )

    @classmethod
    def of(cls, t: ApiToken, secret: str | None = None) -> "PersonalTokenOut":
        """From the entity (a personal token always has one grant and an expiry)."""
        grant = t.token_grants[0]
        assert t.expires_at is not None  # noqa: S101 - personal tokens always expire
        return cls(
            id=t.id,
            name=t.name,
            prefix=t.prefix,
            scopes=list(grant.permissions),
            level=LevelOut(type=grant.level.type.value, id=grant.level.id),
            created_at=t.created_at,
            expires_at=t.expires_at,
            last_used_at=t.last_used_at,
            revoked_at=t.revoked_at,
            token=secret,
        )


class ApiTokensPurgedOut(BaseModel):
    """Revoked API tokens deleted for good."""

    deleted: int = Field(description="How many revoked tokens were deleted")
    ids: list[UUID] = Field(description="Ids of the deleted tokens")


class AuditOut(BaseModel):
    """An audit entry."""

    id: int
    at: datetime
    action: str
    outcome: str
    actor_subject: str | None
    actor_kind: str | None
    actor_name: str | None
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    ip: str | None
    correlation_id: str | None

    @classmethod
    def of(cls, e: AuditEntry) -> "AuditOut":
        """From the entity."""
        return cls(
            id=e.id or 0,
            at=e.at,
            action=e.action,
            outcome=e.outcome,
            actor_subject=e.actor_subject,
            actor_kind=e.actor_kind,
            actor_name=e.actor_name,
            target_type=e.target_type,
            target_id=e.target_id,
            details=e.details,
            ip=e.ip,
            correlation_id=e.correlation_id,
        )


class ReconcileOut(BaseModel):
    """What a reconcile changed in OpenFGA."""

    writes: int
    deletes: int
    changed_subjects: list[str]


# --- passkeys (ADR 0050) ---------------------------------------------------------------------

MAX_CREDENTIAL_JSON = 16_384
"""Upper bound of a serialised WebAuthn response (real ones are 1 to 3 KB)."""


def _bounded_credential(value: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(value, separators=(",", ":"))) > MAX_CREDENTIAL_JSON:
        msg = "credential is too large"
        raise ValueError(msg)
    return value


WebAuthnResponse = Annotated[
    dict[str, Any],
    AfterValidator(_bounded_credential),
    Field(
        description="The PublicKeyCredential as JSON (PublicKeyCredential.toJSON(): id, rawId, "
        "type, response with base64url fields, clientExtensionResults)"
    ),
]
CodeChallenge = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$"),
    Field(description="PKCE S256 challenge of the app: BASE64URL(SHA256(code_verifier))"),
]


class LoginMethodsOut(BaseModel):
    """Login methods this installation offers (the login page shows what is on)."""

    password: bool = True
    passkey: bool = Field(description="Passkeys are configured (MOWDECK_IDENTITY_PUBLIC_URL)")
    github: bool = Field(
        default=False,
        description="Sign in with GitHub is configured (only this flag; the GitHub settings "
        "are never published)",
    )
    apple: bool = Field(
        default=False,
        description="Sign in with Apple is configured (only this flag; the Apple settings "
        "are never published)",
    )


class PasskeyOptionsOut(BaseModel):
    """Options of a WebAuthn ceremony and the id that answers them."""

    ceremony_id: UUID
    public_key: dict[str, Any] = Field(
        description="PublicKeyCredentialCreationOptions or ...RequestOptions as JSON (base64url "
        "binary fields), for PublicKeyCredential.parseCreationOptionsFromJSON / "
        "parseRequestOptionsFromJSON"
    )


class StepUpPasswordIn(In):
    """``POST /me/step-up/password``: the caller's current password."""

    password: Password


class StepUpPasskeyIn(In):
    """``POST /me/step-up/passkey``: the answer to a challenge of the options endpoint."""

    ceremony_id: UUID
    credential: WebAuthnResponse


class PasskeyLoginIn(In):
    """``POST /passkeys/session``: the answer to a login challenge."""

    ceremony_id: UUID
    credential: WebAuthnResponse
    account_id: UUID | None = Field(default=None, description="Account to act for (default: own)")


class AppLoginStartIn(In):
    """``POST /app-login/start``: the native app registers its PKCE challenge."""

    code_challenge: CodeChallenge


class AppLoginStartOut(BaseModel):
    """The app's sign-in request: its id goes into the sign-in page's address."""

    request_id: UUID
    expires_in: int


class AppCodeIn(In):
    """``POST /app-login/code``: hand the fresh browser login to the native app."""

    request_id: UUID = Field(description="The app's sign-in request (from /app-login/start)")


class AppCodeOut(BaseModel):
    """A one-time code for the native app (redeemed with the verifier, valid for seconds)."""

    code: str
    expires_in: int


class AppSessionIn(In):
    """``POST /app-login/session``: the app redeems its code with the PKCE verifier."""

    code: str = Field(min_length=1, max_length=100)
    code_verifier: str = Field(min_length=43, max_length=128)


class PasskeyRegistrationOptionsIn(In):
    """``POST /me/passkeys/options``: the current password (re-authentication), or none after a
    step-up confirmation."""

    password: Password | None = None


class PasskeyRegistrationIn(In):
    """``POST /me/passkeys``: the new credential."""

    ceremony_id: UUID
    credential: WebAuthnResponse
    name: str | None = Field(default=None, max_length=64)


class PasskeyPatch(In):
    """``PATCH /me/passkeys/{id}``."""

    name: str = Field(min_length=1, max_length=64)


class PasskeyOut(BaseModel):
    """A passkey (never with its public key or credential id)."""

    id: UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None
    synced: bool = Field(description="A multi-device passkey that may be synced (backup eligible)")
    backed_up: bool = Field(description="Synced as of the last use")
    aaguid: str | None = Field(description="Model of the authenticator, if it told")
    transports: list[str]
    user_agent: str | None = Field(description="Browser of the registration (a device hint)")

    @classmethod
    def of(cls, p: Passkey) -> "PasskeyOut":
        """From the entity."""
        return cls(
            id=p.id,
            name=p.name,
            created_at=p.created_at,
            last_used_at=p.last_used_at,
            synced=p.backup_eligible,
            backed_up=p.backed_up,
            aaguid=p.aaguid,
            transports=p.transports,
            user_agent=p.created_user_agent,
        )


class OAuthStartIn(In):
    """``POST /oauth/{provider}/start``: the sign-in page's query to come back to."""

    return_query: str | None = Field(
        default=None,
        max_length=1024,
        description="Query of /login to return to; only next (a local path), app_request and "
        "app_state are kept",
    )


class OAuthStartOut(BaseModel):
    """Where to send the browser for the provider's sign-in (the browser keeps a cookie that
    binds the round trip to it)."""

    authorize_url: str
    expires_in: int = Field(description="Seconds the round trip stays usable")


class ExternalLinkIn(In):
    """``POST /me/external-identities/{provider}/link``: the current password again, or none
    after a step-up confirmation."""

    password: Password | None = None


class ExternalIdentityOut(BaseModel):
    """A linked account of a login provider (never the provider's settings)."""

    provider: str
    display_name: str | None = Field(description="The account's name at the provider (login)")
    linked_at: datetime
    last_used_at: datetime | None

    @classmethod
    def of(cls, link: ExternalIdentity) -> "ExternalIdentityOut":
        """From the entity."""
        return cls(
            provider=link.provider,
            display_name=link.display_name,
            linked_at=link.linked_at,
            last_used_at=link.last_used_at,
        )


class WidgetCredentialIn(In):
    """``POST /me/widget-credentials``."""

    device_name: str = Field(
        min_length=1,
        max_length=80,
        examples=["Lukas' iPhone"],
        description="Shown in the account's token list as `iOS widgets: <device_name>`.",
    )
    replaces: UUID | None = Field(
        default=None,
        description="An earlier widget credential of the caller on this device: revoked in the "
        "same transaction (rotation).",
    )


class WidgetCredentialOut(BaseModel):
    """A widget credential: an API token for the iOS widgets (never with its secret, except once
    at creation)."""

    id: UUID
    name: str
    permissions: list[str]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    token: str | None = Field(
        default=None,
        description="The secret (`mdk_...`), returned only at creation. Exchange it at "
        "`POST /token` for 10 minute access tokens.",
    )

    @classmethod
    def of(cls, t: ApiToken, secret: str | None = None) -> "WidgetCredentialOut":
        """From the entity."""
        return cls(
            id=t.id,
            name=t.name,
            permissions=t.permissions,
            created_at=t.created_at,
            expires_at=t.expires_at,
            last_used_at=t.last_used_at,
            revoked_at=t.revoked_at,
            token=secret,
        )
