"""Authentication plug-ins: every login method sits behind one of two interfaces.

* :class:`CredentialAuthenticator`: credentials in the request (password, API token).
* :class:`RedirectAuthenticator`: a browser round trip to a provider (GitHub, OIDC). Phase 1
  defines the interface and registers a GitHub stub behind a feature flag that is off.

The :class:`AuthenticatorRegistry` holds the enabled plug-ins by name; the API looks them up,
so a new method is a new class plus one registration.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from identity.application.ports import Clock, PasswordHasher, Store
from identity.domain.credentials import normalize_username, parse_api_token, same_hash
from identity.domain.models import AccountStatus, Status


@dataclass(frozen=True, slots=True)
class AuthResult:
    """Outcome of an authentication. ``reason`` is internal (logs, audit), never shown."""

    ok: bool
    subject_id: UUID | None = None
    method: str = ""
    reason: str | None = None
    needs_rehash: bool = False


@runtime_checkable
class CredentialAuthenticator(Protocol):
    """Checks credentials sent with the request."""

    name: str

    async def authenticate(self, credentials: Mapping[str, str]) -> AuthResult:
        """Verify ``credentials``; never raises for wrong credentials."""
        ...


@runtime_checkable
class RedirectAuthenticator(Protocol):
    """A login through a provider's web page (OAuth 2 / OIDC)."""

    name: str

    async def start(self, return_to: str) -> str:
        """The provider URL to redirect the browser to."""
        ...

    async def callback(self, params: Mapping[str, str]) -> AuthResult:
        """Finish the login from the provider's callback parameters."""
        ...


class PasswordAuthenticator:
    """Username and password (Argon2id). Unknown users cost the same time as known ones."""

    name = "password"

    def __init__(self, store: Store, hasher: PasswordHasher) -> None:
        self.store = store
        self.hasher = hasher

    async def authenticate(self, credentials: Mapping[str, str]) -> AuthResult:
        """``{"username", "password"}``."""
        username = normalize_username(credentials.get("username", ""))
        password = credentials.get("password", "")
        async with self.store.transaction() as tx:
            found = await tx.users.get(username_norm=username)
        if found is None or found.password_hash is None:
            await self.hasher.dummy_verify(password)
            return AuthResult(False, method=self.name, reason="unknown user")
        if not await self.hasher.verify(found.password_hash, password):
            return AuthResult(False, found.id, self.name, "wrong password")
        if found.status is not Status.ACTIVE:
            return AuthResult(False, found.id, self.name, "user disabled")
        return AuthResult(
            True, found.id, self.name, needs_rehash=self.hasher.needs_rehash(found.password_hash)
        )


class ApiTokenAuthenticator:
    """API tokens ``mdk_<id>_<secret>``: lookup by id, constant-time hash comparison."""

    name = "api_token"

    def __init__(self, store: Store, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    async def authenticate(self, credentials: Mapping[str, str]) -> AuthResult:
        """``{"token"}``."""
        parsed = parse_api_token(credentials.get("token", ""))
        if parsed is None:
            return AuthResult(False, method=self.name, reason="malformed token")
        token_id, secret = parsed
        async with self.store.transaction() as tx:
            token = await tx.api_tokens.get(id=token_id)
            acct = await tx.accounts.get(id=token.account_id) if token else None
        if token is None or not same_hash(token.secret_hash, secret):
            return AuthResult(False, method=self.name, reason="unknown token")
        if not token.is_usable(self.clock()):
            return AuthResult(False, token.id, self.name, "token revoked or expired")
        if acct is None or acct.status is not AccountStatus.ACTIVE:
            return AuthResult(False, token.id, self.name, "account not active")
        return AuthResult(True, token.id, self.name)


class GitHubAuthenticator:
    """GitHub login (design section 3.3). Not implemented in Phase 1; off by default."""

    name = "github"

    async def start(self, return_to: str) -> str:
        """Not available yet."""
        raise NotImplementedError("GitHub login is planned for a later phase")

    async def callback(self, params: Mapping[str, str]) -> AuthResult:
        """Not available yet."""
        raise NotImplementedError("GitHub login is planned for a later phase")


class AuthenticatorRegistry:
    """The enabled authenticators of this installation, by name."""

    def __init__(self) -> None:
        self.credential: dict[str, CredentialAuthenticator] = {}
        self.redirect: dict[str, RedirectAuthenticator] = {}

    def register(self, authenticator: Any) -> None:
        """Add a credential or redirect authenticator."""
        if isinstance(authenticator, CredentialAuthenticator):
            self.credential[authenticator.name] = authenticator
        elif isinstance(authenticator, RedirectAuthenticator):
            self.redirect[authenticator.name] = authenticator
        else:
            msg = f"{authenticator!r} is not an authenticator"
            raise TypeError(msg)

    def get(self, name: str) -> CredentialAuthenticator | None:
        """The credential authenticator ``name`` if enabled."""
        return self.credential.get(name)

    @property
    def names(self) -> list[str]:
        """Names of all enabled authenticators."""
        return sorted([*self.credential, *self.redirect])
