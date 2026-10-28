"""Ports: what the use cases need from the outside world, as protocols.

Persistence is one generic :class:`Table` per entity inside a :class:`Tx` (a database
transaction). Filters are keyword arguments: plain values compare for equality (``None`` means
``IS NULL``), :class:`Gt`, :class:`Ge`, :class:`Lt` and :class:`In` express the rest. Two stores
implement it: PostgreSQL (``adapters.db``) and memory (``adapters.memory``, tests).
"""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from mowdeck_contracts.v1 import ModuleDescriptor
from pydantic import BaseModel

from identity.domain.models import (
    Account,
    ApiToken,
    AppLoginCode,
    AppLoginRequest,
    AuditEntry,
    CatalogueEntry,
    ExternalIdentity,
    LoginAttempt,
    Membership,
    OAuthState,
    OnboardingProgress,
    Passkey,
    RelyingParty,
    Role,
    RoleAssignment,
    Session,
    SigningKeyRecord,
    User,
    WebAuthnChallenge,
)


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


@dataclass(frozen=True, slots=True)
class Gt:
    """Filter: column greater than ``value``."""

    value: Any


@dataclass(frozen=True, slots=True)
class Ge:
    """Filter: column greater than or equal to ``value``."""

    value: Any


@dataclass(frozen=True, slots=True)
class Lt:
    """Filter: column less than ``value``."""

    value: Any


@dataclass(frozen=True, slots=True)
class In:
    """Filter: column is one of ``values``."""

    values: Sequence[Any]


class Table[T](Protocol):
    """Rows of one entity type."""

    async def add(self, item: T) -> T:
        """Insert; returns the item (with a generated id where the store assigns one).

        Raises ``ConflictError`` when a unique constraint is violated.
        """
        ...

    async def get(self, **where: Any) -> T | None:
        """The single row matching ``where``, or None."""
        ...

    async def find(
        self,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        **where: Any,
    ) -> list[T]:
        """All rows matching ``where``."""
        ...

    async def update(self, item: T) -> None:
        """Write back a changed row (identified by its primary key)."""
        ...

    async def delete(self, **where: Any) -> int:
        """Delete matching rows; returns the number deleted."""
        ...

    async def count(self, **where: Any) -> int:
        """Number of matching rows."""
        ...


class Tx(Protocol):
    """One transaction: all tables, committed together or not at all."""

    @property
    def accounts(self) -> Table[Account]:
        """The accounts table."""
        ...

    @property
    def users(self) -> Table[User]:
        """The users table."""
        ...

    @property
    def memberships(self) -> Table[Membership]:
        """The memberships table."""
        ...

    @property
    def roles(self) -> Table[Role]:
        """The roles table."""
        ...

    @property
    def assignments(self) -> Table[RoleAssignment]:
        """The assignments table."""
        ...

    @property
    def sessions(self) -> Table[Session]:
        """The sessions table."""
        ...

    @property
    def api_tokens(self) -> Table[ApiToken]:
        """The api tokens table."""
        ...

    @property
    def signing_keys(self) -> Table[SigningKeyRecord]:
        """The signing keys table."""
        ...

    @property
    def audit(self) -> Table[AuditEntry]:
        """The audit table."""
        ...

    @property
    def login_attempts(self) -> Table[LoginAttempt]:
        """The login attempts table."""
        ...

    @property
    def catalogue(self) -> Table[CatalogueEntry]:
        """The catalogue table."""
        ...

    @property
    def passkeys(self) -> Table[Passkey]:
        """The passkeys table."""
        ...

    @property
    def webauthn_challenges(self) -> Table[WebAuthnChallenge]:
        """The WebAuthn challenges table."""
        ...

    @property
    def app_login_codes(self) -> Table[AppLoginCode]:
        """The app login codes table."""
        ...

    @property
    def app_login_requests(self) -> Table[AppLoginRequest]:
        """The app login requests table."""
        ...

    @property
    def external_identities(self) -> Table[ExternalIdentity]:
        """The external identities table (linked GitHub accounts)."""
        ...

    @property
    def oauth_states(self) -> Table[OAuthState]:
        """The OAuth round trips table (state and PKCE challenge)."""
        ...

    @property
    def onboarding(self) -> Table[OnboardingProgress]:
        """The setup tour progress table (one row per user)."""
        ...


class Store(Protocol):
    """Opens transactions. Leaving the context commits; an exception rolls back."""

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        """A new transaction."""
        ...


class PasswordHasher(Protocol):
    """Slow, salted password hashing (Argon2id)."""

    async def hash(self, password: str) -> str:
        """Hash a password."""
        ...

    async def verify(self, password_hash: str, password: str) -> bool:
        """True if ``password`` matches; never raises for a mismatch."""
        ...

    def needs_rehash(self, password_hash: str) -> bool:
        """True if the hash was made with other parameters than the current ones."""
        ...

    async def dummy_verify(self, password: str) -> None:
        """Spend the time of a verification (unknown users must not answer faster)."""
        ...


class KeyCipher(Protocol):
    """Encrypts private signing keys at rest."""

    def encrypt(self, kid: str, raw: bytes) -> bytes:
        """Encrypt ``raw`` bound to ``kid``."""
        ...

    def decrypt(self, kid: str, blob: bytes) -> bytes:
        """Inverse of :meth:`encrypt`; raises on a wrong key or tampering."""
        ...


class EventPublisher(Protocol):
    """Publishes domain events (``event.identity.*``)."""

    async def publish(self, event_type: str, account_id: UUID | None, payload: BaseModel) -> None:
        """Publish one event; failures are logged, never raised."""
        ...


@dataclass(frozen=True, slots=True)
class FetchedDescriptor:
    """A module descriptor fetched from one URL (None if the module did not answer)."""

    url: str
    descriptor: ModuleDescriptor | None


class ModuleSource(Protocol):
    """Fetches the module descriptors of the installation's services."""

    async def fetch(self) -> list[FetchedDescriptor]:
        """One entry per configured module URL."""
        ...


class CeremonyRejected(Exception):  # noqa: N818 - reads like what happened
    """A WebAuthn response failed verification. ``reason`` is internal (logs, audit)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class KnownCredential:
    """A credential the user already has (excluded from a new registration)."""

    credential_id: bytes
    transports: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NewCredential:
    """A verified registration: what is stored as a :class:`Passkey`."""

    credential_id: bytes
    public_key: bytes
    sign_count: int
    transports: tuple[str, ...]
    aaguid: str | None
    backup_eligible: bool
    backed_up: bool


@dataclass(frozen=True, slots=True)
class AssertionClaim:
    """What an authentication response claims before it is verified: which credential, and the
    user handle the authenticator returned (always present for discoverable credentials)."""

    credential_id: bytes
    user_handle: bytes | None


@dataclass(frozen=True, slots=True)
class VerifiedAssertion:
    """A verified authentication response."""

    sign_count: int
    backed_up: bool


class PasskeyCeremonies(Protocol):
    """The WebAuthn ceremonies (options and verification), implemented with a WebAuthn library.

    Every option set requires user verification and discoverable credentials; verification
    checks the challenge, the RP ID hash, the origin, user presence and user verification.
    Raises :class:`CeremonyRejected` for anything malformed or wrong.
    """

    def registration_options(
        self,
        rp: RelyingParty,
        *,
        challenge: bytes,
        user_handle: bytes,
        user_name: str,
        display_name: str,
        exclude: Sequence[KnownCredential],
        timeout_ms: int,
    ) -> dict[str, Any]:
        """``PublicKeyCredentialCreationOptions`` as JSON (WebAuthn level 3 encoding)."""
        ...

    def verify_registration(
        self, rp: RelyingParty, *, credential: Mapping[str, Any], challenge: bytes
    ) -> NewCredential:
        """Verify a registration response (``PublicKeyCredential.toJSON()``)."""
        ...

    def authentication_options(
        self, rp: RelyingParty, *, challenge: bytes, timeout_ms: int
    ) -> dict[str, Any]:
        """``PublicKeyCredentialRequestOptions`` as JSON, without ``allowCredentials``."""
        ...

    def read_assertion(self, credential: Mapping[str, Any]) -> AssertionClaim:
        """Parse an authentication response far enough to find the credential."""
        ...

    def verify_assertion(
        self,
        rp: RelyingParty,
        *,
        credential: Mapping[str, Any],
        challenge: bytes,
        public_key: bytes,
    ) -> VerifiedAssertion:
        """Verify an authentication response against the stored public key.

        The signature counter is returned, not judged: the caller compares it with the stored
        one (clone detection is a policy of the relying party).
        """
        ...


class ProviderRejected(Exception):  # noqa: N818 - reads like what happened
    """The login provider refused the authorization code or answered nonsense (a bad or reused
    code, a wrong PKCE verifier, an account without an id). ``reason`` is internal."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ProviderUnavailable(Exception):  # noqa: N818 - reads like what happened
    """The login provider could not be reached or failed (network, 5xx). ``reason`` is
    internal."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ExternalProfile:
    """Who signed in at the provider: its stable account id and a name to show.

    Deliberately nothing else: MowDeck never matches or links accounts by e-mail address.
    """

    subject: str
    display_name: str | None = None


class OAuthClient(Protocol):
    """The endpoints of one OAuth 2 provider (authorization code flow with PKCE S256).

    The redirect authenticator keeps state, PKCE and the browser binding; the client only builds
    the authorization URL and turns a code into the provider's profile of the signed-in account.
    A later OIDC client implements the same port (discovery, ID token).
    """

    provider: str
    """The provider key (``github``), also the path segment of the callback."""

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        """The provider page the browser is sent to."""
        ...

    async def profile(self, *, code: str, code_verifier: str, redirect_uri: str) -> ExternalProfile:
        """Redeem ``code`` (with the PKCE verifier) and read the account it belongs to.

        Raises :class:`ProviderRejected` or :class:`ProviderUnavailable`.
        """
        ...
