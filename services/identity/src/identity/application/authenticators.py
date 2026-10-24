"""Authentication plug-ins: every login method sits behind one of two interfaces.

* :class:`CredentialAuthenticator`: credentials in the request (password, API token, passkey).
  :class:`ChallengeAuthenticator` is a credential authenticator that first hands out a
  challenge the credentials must answer (passkeys, ADR 0049).
* :class:`RedirectAuthenticator`: a browser round trip to a provider (GitHub, OIDC). Phase 1
  defines the interface and registers a GitHub stub behind a feature flag that is off.

The :class:`AuthenticatorRegistry` holds the enabled plug-ins by name; the API looks them up,
so a new method is a new class plus one registration.
"""

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from identity.application.ports import (
    CeremonyRejected,
    Clock,
    PasskeyCeremonies,
    PasswordHasher,
    Store,
)
from identity.domain.credentials import normalize_username, parse_api_token, same_hash
from identity.domain.models import (
    AccountStatus,
    CeremonyPurpose,
    RelyingParty,
    Status,
    WebAuthnChallenge,
)

CHALLENGE_BYTES = 32
"""Random bytes of a WebAuthn challenge (the spec asks for at least 16)."""


@dataclass(frozen=True, slots=True)
class AuthResult:
    """Outcome of an authentication. ``reason`` is internal (logs, audit), never shown."""

    ok: bool
    subject_id: UUID | None = None
    method: str = ""
    reason: str | None = None
    needs_rehash: bool = False
    credential_ref: str | None = None
    """Which credential of the subject was used (the passkey id), for the audit log."""


@runtime_checkable
class CredentialAuthenticator(Protocol):
    """Checks credentials sent with the request."""

    name: str

    async def authenticate(self, credentials: Mapping[str, str]) -> AuthResult:
        """Verify ``credentials``; never raises for wrong credentials."""
        ...


@dataclass(frozen=True, slots=True)
class Challenge:
    """A challenge handed to the client: its id and the options the client passes on."""

    ceremony_id: UUID
    options: dict[str, Any]


@runtime_checkable
class ChallengeAuthenticator(CredentialAuthenticator, Protocol):
    """A credential authenticator whose credentials answer a challenge issued just before."""

    async def challenge(self) -> Challenge:
        """A new single-use challenge; ``authenticate`` names it by ``ceremony_id``."""
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


class PasskeyAuthenticator:
    """Passwordless login with a discoverable WebAuthn credential (passkey), ADR 0049.

    ``challenge`` stores a single-use login challenge; ``authenticate`` takes
    ``{"ceremony_id", "credential"}`` (the ``PublicKeyCredential`` as JSON text), consumes the
    challenge, finds the passkey by its credential id, checks the user handle, verifies the
    assertion with user verification required, rejects a signature counter that did not grow
    (a cloned authenticator) and records the use. No username is involved, so an answer never
    tells whether a user exists.
    """

    name = "passkey"

    def __init__(
        self,
        store: Store,
        clock: Clock,
        ceremonies: PasskeyCeremonies,
        rp: RelyingParty,
        timeout: timedelta,
    ) -> None:
        self.store = store
        self.clock = clock
        self.ceremonies = ceremonies
        self.rp = rp
        self.timeout = timeout

    async def challenge(self) -> Challenge:
        """Options for ``navigator.credentials.get()`` (empty ``allowCredentials``)."""
        now = self.clock()
        record = WebAuthnChallenge(
            id=uuid4(),
            purpose=CeremonyPurpose.LOGIN,
            challenge=secrets.token_bytes(CHALLENGE_BYTES),
            created_at=now,
            expires_at=now + self.timeout,
        )
        async with self.store.transaction() as tx:
            await tx.webauthn_challenges.add(record)
        options = self.ceremonies.authentication_options(
            self.rp,
            challenge=record.challenge,
            timeout_ms=int(self.timeout.total_seconds() * 1000),
        )
        return Challenge(record.id, options)

    async def authenticate(self, credentials: Mapping[str, str]) -> AuthResult:
        """``{"ceremony_id", "credential"}``."""
        try:
            ceremony_id = UUID(credentials.get("ceremony_id", ""))
            response = json.loads(credentials.get("credential", ""))
        except ValueError:
            return AuthResult(False, method=self.name, reason="malformed request")
        if not isinstance(response, dict):
            return AuthResult(False, method=self.name, reason="malformed request")
        try:
            claim = self.ceremonies.read_assertion(response)
        except CeremonyRejected as exc:
            return AuthResult(False, method=self.name, reason=exc.reason)
        now = self.clock()
        async with self.store.transaction() as tx:
            # Consumed whatever happens next: a challenge answers exactly one attempt.
            record = await tx.webauthn_challenges.get(
                id=ceremony_id, purpose=CeremonyPurpose.LOGIN.value
            )
            taken = await tx.webauthn_challenges.delete(id=ceremony_id) if record else 0
            passkey = await tx.passkeys.get(credential_id=claim.credential_id)
            found = await tx.users.get(id=passkey.user_id) if passkey else None
        if record is None or taken != 1 or now >= record.expires_at:
            return AuthResult(False, method=self.name, reason="unknown or expired challenge")
        if passkey is None or found is None:
            return AuthResult(False, method=self.name, reason="unknown credential")
        ref = str(passkey.id)

        def failed(reason: str) -> AuthResult:
            return AuthResult(False, found.id, self.name, reason, credential_ref=ref)

        if claim.user_handle is not None and claim.user_handle != found.id.bytes:
            return failed("user handle mismatch")
        try:
            verified = self.ceremonies.verify_assertion(
                self.rp,
                credential=response,
                challenge=record.challenge,
                public_key=passkey.public_key,
            )
        except CeremonyRejected as exc:
            return failed(exc.reason)
        if (verified.sign_count > 0 or passkey.sign_count > 0) and (
            verified.sign_count <= passkey.sign_count
        ):
            # WebAuthn section 6.1.1: a counter that did not grow hints at a cloned
            # authenticator. Policy: refuse this login (audited), keep the passkey.
            return failed("signature counter did not increase")
        if found.status is not Status.ACTIVE:
            return failed("user disabled")
        async with self.store.transaction() as tx:
            fresh = await tx.passkeys.get(id=passkey.id)
            if fresh is None:
                return failed("passkey removed")
            fresh.sign_count = max(fresh.sign_count, verified.sign_count)
            fresh.backed_up = verified.backed_up
            fresh.last_used_at = now
            await tx.passkeys.update(fresh)
        return AuthResult(True, found.id, self.name, credential_ref=ref)


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

    def challenger(self, name: str) -> ChallengeAuthenticator | None:
        """The challenge authenticator ``name`` if enabled."""
        found = self.credential.get(name)
        return found if isinstance(found, ChallengeAuthenticator) else None

    @property
    def names(self) -> list[str]:
        """Names of all enabled authenticators."""
        return sorted([*self.credential, *self.redirect])
