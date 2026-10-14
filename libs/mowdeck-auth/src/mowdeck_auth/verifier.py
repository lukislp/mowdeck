"""Verifying access tokens locally: no call to Identity per request.

Rules (in this order; the first failure rejects the token with a :class:`TokenError`):

1. At most 8 KiB, three base64url parts, a JSON header.
2. ``alg`` is in the allow list (``EdDSA``); ``none`` and HMAC are never accepted, so the
   public key cannot be abused as an HMAC secret.
3. ``typ`` is ``at+jwt`` and a ``kid`` is present.
4. The key for ``kid`` is known (an unknown kid triggers one JWKS refresh) and is an OKP
   Ed25519 key.
5. Signature, ``iss``, ``aud``, ``exp``, ``nbf`` and ``iat`` with ``leeway``; the claims
   ``sub``, ``kind``, ``iss``, ``aud``, ``iat``, ``exp`` and ``jti`` are required.
6. The claims form a valid :class:`~mowdeck_contracts.v1.Principal`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from jwt.exceptions import InvalidTokenError, PyJWKError
from mowdeck_contracts.v1 import Principal
from pydantic import ValidationError

from mowdeck_auth.claims import REQUIRED_CLAIMS, principal_from_claims
from mowdeck_auth.issuer import ACCESS_TOKEN_TYPE
from mowdeck_auth.jwks import JwksCache, KeySource
from mowdeck_auth.keys import ALLOWED_ALGORITHMS
from mowdeck_auth.settings import DEFAULT_AUDIENCE, DEFAULT_ISSUER, AuthSettings

MAX_TOKEN_LENGTH = 8192
"""Longer bearer values are rejected before any parsing."""


class TokenError(Exception):
    """The token is not acceptable. ``reason`` is for logs only, never for the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """A verified access token: the principal plus the expiry of the token.

    Long-lived connections (Realtime's WebSockets) need ``expires_at`` to end the connection
    when the token runs out; request handlers only need the principal.
    """

    principal: Principal
    expires_at: datetime
    """``exp`` of the token (UTC), without the leeway."""


class JwtVerifier:
    """Verifies MowDeck access tokens against a :class:`KeySource`."""

    def __init__(
        self,
        keys: KeySource,
        *,
        issuer: str = DEFAULT_ISSUER,
        audience: str = DEFAULT_AUDIENCE,
        leeway: timedelta = timedelta(seconds=30),
        algorithms: Sequence[str] = ALLOWED_ALGORITHMS,
    ) -> None:
        unsafe = {alg for alg in algorithms if alg == "none" or alg.startswith("HS")}
        if unsafe:
            msg = f"refusing unsafe algorithms {sorted(unsafe)}"
            raise ValueError(msg)
        self.keys = keys
        self.issuer = issuer
        self.audience = audience
        self.leeway = leeway
        self.algorithms = tuple(algorithms)

    @classmethod
    def from_settings(cls, settings: AuthSettings) -> "JwtVerifier":
        """A verifier with a :class:`JwksCache` built from ``settings``."""
        cache = JwksCache(
            settings.jwks_url,
            lifespan=settings.jwks_cache_seconds,
            refresh_cooldown=settings.jwks_refresh_cooldown_seconds,
        )
        return cls(
            cache,
            issuer=settings.issuer,
            audience=settings.audience,
            leeway=timedelta(seconds=settings.leeway_seconds),
        )

    async def verify(self, token: str) -> Principal:
        """The principal of a valid token; :class:`TokenError` otherwise."""
        return (await self.verify_token(token)).principal

    async def verify_token(self, token: str) -> VerifiedToken:
        """The principal and the expiry of a valid token; :class:`TokenError` otherwise."""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise TokenError("token missing or too long")
        try:
            header = jwt.get_unverified_header(token)
        except InvalidTokenError as exc:
            raise TokenError(f"malformed token: {type(exc).__name__}") from None
        alg = header.get("alg")
        if alg not in self.algorithms:
            raise TokenError(f"algorithm {alg!r} is not allowed")
        typ = str(header.get("typ", "")).lower().removeprefix("application/")
        if typ != ACCESS_TOKEN_TYPE:
            raise TokenError(f"token type {typ!r} is not an access token")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenError("token has no kid")
        key = await self.keys.key_for(kid)
        if key is None:
            raise TokenError("unknown signing key")
        if key.key_type != "OKP" or key.algorithm_name not in self.algorithms:
            raise TokenError("signing key is not an Ed25519 key")
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[alg],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except (InvalidTokenError, PyJWKError) as exc:
            raise TokenError(f"invalid token: {type(exc).__name__}") from None
        try:
            principal = principal_from_claims(claims)
            expires_at = datetime.fromtimestamp(int(claims["exp"]), UTC)
        except (ValidationError, ValueError, TypeError, OverflowError) as exc:
            raise TokenError(f"invalid claims: {type(exc).__name__}") from None
        return VerifiedToken(principal=principal, expires_at=expires_at)
