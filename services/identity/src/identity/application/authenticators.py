"""Authentication plug-ins: every login method sits behind one of two interfaces.

* :class:`CredentialAuthenticator`: credentials in the request (password, API token, passkey).
  :class:`ChallengeAuthenticator` is a credential authenticator that first hands out a
  challenge the credentials must answer (passkeys, ADR 0050).
* :class:`RedirectAuthenticator`: a browser round trip to a provider. :class:`OAuthAuthenticator`
  implements it for any OAuth 2 provider (authorization code flow with PKCE S256, single-use
  state bound to the browser) on top of an ``OAuthClient`` port; GitHub is the first one
  (:class:`GitHubAuthenticator`, ADR 0051), a generic OIDC client can follow with the same class.

The :class:`AuthenticatorRegistry` holds the enabled plug-ins by name; the API looks them up,
so a new method is a new class plus one registration.
"""

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from mowdeck_service_kit import get_logger

from identity.application.ports import (
    CeremonyRejected,
    Clock,
    ExternalProfile,
    OAuthClient,
    PasskeyCeremonies,
    PasswordHasher,
    ProviderRejected,
    ProviderUnavailable,
    Store,
)
from identity.domain.credentials import (
    new_oauth_state,
    new_pkce_verifier,
    normalize_username,
    parse_api_token,
    s256_challenge,
    same_hash,
    sha256_hex,
    verifier_matches,
)
from identity.domain.models import (
    AccountStatus,
    CeremonyPurpose,
    OAuthState,
    RedirectPurpose,
    RelyingParty,
    Status,
    WebAuthnChallenge,
)

_log = get_logger("identity.authenticators")

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


@dataclass(frozen=True, slots=True)
class RedirectStart:
    """Where to send the browser, and the secret it keeps until the provider sends it back.

    ``browser_secret`` (the PKCE verifier) goes into an HttpOnly cookie; only its hash is
    stored, so the round trip can be finished only in the browser that started it.
    """

    url: str
    browser_secret: str = field(repr=False)
    expires_in: int


class RedirectFailure(StrEnum):
    """Why a round trip ended without a result; the web app shows a message per code."""

    CANCELLED = "cancelled"
    """The user declined at the provider."""
    EXPIRED = "expired"
    """Unknown, used or expired state, or another browser: start again."""
    DENIED = "denied"
    """The provider account is not linked to an active user, or the code was refused."""
    UNAVAILABLE = "unavailable"
    """The provider failed or the installation's provider settings are wrong (see the log)."""


@dataclass(frozen=True, slots=True)
class RedirectCallback:
    """What a provider's callback established.

    ``user_id`` is the user who started a link, or the linked user of a successful login (and,
    for a refused login of a known user, that user, for the audit log). ``reason`` is internal.
    """

    provider: str
    purpose: RedirectPurpose | None = None
    user_id: UUID | None = None
    profile: ExternalProfile | None = None
    return_query: str | None = None
    identity_id: UUID | None = None
    failure: RedirectFailure | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        """The round trip succeeded."""
        return self.failure is None


@runtime_checkable
class RedirectAuthenticator(Protocol):
    """A login (or the link of a provider account) through a provider's web page."""

    name: str

    async def start(
        self,
        purpose: RedirectPurpose,
        *,
        user_id: UUID | None = None,
        return_query: str | None = None,
    ) -> RedirectStart:
        """A new single-use round trip: the provider URL and the browser's secret."""
        ...

    async def callback(
        self, params: Mapping[str, str], browser_secret: str | None
    ) -> RedirectCallback:
        """Finish the round trip from the provider's callback parameters; never raises for
        anything the browser or the provider sent."""
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
    """Passwordless login with a discoverable WebAuthn credential (passkey), ADR 0050.

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


class OAuthAuthenticator:
    """Login and account linking through an OAuth 2 provider (ADR 0051).

    Authorization code flow with PKCE (S256). ``start`` stores a single-use round trip
    (:class:`OAuthState`: the SHA-256 of the random ``state``, the S256 hash of the PKCE
    verifier, the purpose and, for a link, the user) and hands the verifier to the browser as a
    secret it keeps in a cookie. ``callback`` consumes the round trip whatever happens, requires
    that same browser (the verifier must match), redeems the code with the verifier and reads
    the provider's account id. A login then finds the user only through an explicit link
    (``external_identities``: provider and the provider's stable account id); nothing is ever
    matched by e-mail address, and an unknown account is refused, never registered.
    """

    def __init__(
        self,
        client: OAuthClient,
        store: Store,
        clock: Clock,
        *,
        redirect_uri: str,
        ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        self.name = client.provider
        self.client = client
        self.store = store
        self.clock = clock
        self.redirect_uri = redirect_uri
        self.ttl = ttl

    async def start(
        self,
        purpose: RedirectPurpose,
        *,
        user_id: UUID | None = None,
        return_query: str | None = None,
    ) -> RedirectStart:
        """A login (``return_query``: the sign-in page's query) or a link (``user_id``)."""
        if (purpose is RedirectPurpose.LINK) != (user_id is not None):
            msg = "a link needs the user, a login none"
            raise ValueError(msg)
        now = self.clock()
        state, verifier = new_oauth_state(), new_pkce_verifier()
        challenge = s256_challenge(verifier)
        record = OAuthState(
            id=uuid4(),
            state_hash=sha256_hex(state),
            provider=self.name,
            purpose=purpose,
            code_challenge=challenge,
            created_at=now,
            expires_at=now + self.ttl,
            user_id=user_id,
            return_query=return_query if purpose is RedirectPurpose.LOGIN else None,
        )
        async with self.store.transaction() as tx:
            await tx.oauth_states.add(record)
        url = self.client.authorization_url(
            state=state, code_challenge=challenge, redirect_uri=self.redirect_uri
        )
        return RedirectStart(url, verifier, int(self.ttl.total_seconds()))

    async def callback(
        self, params: Mapping[str, str], browser_secret: str | None
    ) -> RedirectCallback:
        """``{state, code}`` or ``{state, error}`` from the provider; the browser's secret."""
        state = params.get("state", "")
        if not state or len(state) > 256:
            return RedirectCallback(self.name, failure=RedirectFailure.EXPIRED, reason="no state")
        now = self.clock()
        async with self.store.transaction() as tx:
            # Consumed whatever happens next: a round trip answers exactly one callback.
            record = await tx.oauth_states.get(state_hash=sha256_hex(state), provider=self.name)
            taken = await tx.oauth_states.delete(id=record.id) if record else 0
        if record is None or taken != 1:
            return RedirectCallback(
                self.name, failure=RedirectFailure.EXPIRED, reason="unknown state"
            )
        if now >= record.expires_at:
            return self._failed(record, RedirectFailure.EXPIRED, "expired state")
        if not browser_secret or not verifier_matches(record.code_challenge, browser_secret):
            return self._failed(record, RedirectFailure.EXPIRED, "other browser (no verifier)")
        error = params.get("error", "")[:100]
        if error == "access_denied":
            return self._failed(record, RedirectFailure.CANCELLED, "declined at the provider")
        if error:
            _log.warning(
                "the login provider answered with an error", provider=self.name, error=error
            )
            return self._failed(record, RedirectFailure.UNAVAILABLE, f"provider error {error}")
        code = params.get("code", "")
        if not code or len(code) > 512:
            return self._failed(record, RedirectFailure.DENIED, "no code")
        try:
            profile = await self.client.profile(
                code=code, code_verifier=browser_secret, redirect_uri=self.redirect_uri
            )
        except ProviderRejected as exc:
            return self._failed(record, RedirectFailure.DENIED, f"code refused: {exc.reason}")
        except ProviderUnavailable as exc:
            _log.warning("the login provider failed", provider=self.name, reason=exc.reason)
            return self._failed(record, RedirectFailure.UNAVAILABLE, f"unavailable: {exc.reason}")
        if record.purpose is RedirectPurpose.LINK:
            return RedirectCallback(
                self.name, purpose=record.purpose, user_id=record.user_id, profile=profile
            )
        return await self._linked_user(record, profile, now)

    def _failed(
        self, record: OAuthState, failure: RedirectFailure, reason: str
    ) -> RedirectCallback:
        return RedirectCallback(
            self.name,
            purpose=record.purpose,
            user_id=record.user_id,
            return_query=record.return_query,
            failure=failure,
            reason=reason,
        )

    async def _linked_user(
        self, record: OAuthState, profile: ExternalProfile, now: datetime
    ) -> RedirectCallback:
        """A login: the user the provider account is explicitly linked to, if active."""
        async with self.store.transaction() as tx:
            link = await tx.external_identities.get(provider=self.name, subject=profile.subject)
            found = await tx.users.get(id=link.user_id) if link else None
            if link is not None and found is not None and found.status is Status.ACTIVE:
                link.last_used_at = now
                if profile.display_name:
                    link.display_name = profile.display_name
                await tx.external_identities.update(link)
        if link is None or found is None:
            return RedirectCallback(
                self.name,
                purpose=record.purpose,
                profile=profile,
                return_query=record.return_query,
                failure=RedirectFailure.DENIED,
                reason="account not linked",
            )
        active = found.status is Status.ACTIVE
        return RedirectCallback(
            self.name,
            purpose=record.purpose,
            user_id=found.id,
            profile=profile,
            return_query=record.return_query,
            identity_id=link.id,
            failure=None if active else RedirectFailure.DENIED,
            reason=None if active else "user disabled",
        )


class GitHubAuthenticator(OAuthAuthenticator):
    """Sign in with GitHub (ADR 0051): the OAuth flow above with the GitHub client
    (``adapters/github.py``, scope ``read:user``); the account is GitHub's numeric user id."""

    def __init__(
        self,
        client: OAuthClient,
        store: Store,
        clock: Clock,
        *,
        redirect_uri: str,
        ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        if client.provider != "github":
            msg = f"not a GitHub client: {client.provider!r}"
            raise ValueError(msg)
        super().__init__(client, store, clock, redirect_uri=redirect_uri, ttl=ttl)


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

    def redirector(self, name: str) -> RedirectAuthenticator | None:
        """The redirect authenticator ``name`` if enabled."""
        return self.redirect.get(name)

    def challenger(self, name: str) -> ChallengeAuthenticator | None:
        """The challenge authenticator ``name`` if enabled."""
        found = self.credential.get(name)
        return found if isinstance(found, ChallengeAuthenticator) else None

    @property
    def names(self) -> list[str]:
        """Names of all enabled authenticators."""
        return sorted([*self.credential, *self.redirect])
