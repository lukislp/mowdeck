"""Signing access tokens (used by Identity and by tests).

::

    issuer = TokenIssuer(lambda: key_store.active_key, issuer="urn:mowdeck:identity")
    issued = issuer.issue(principal)  # IssuedToken(token, token_id, expires_at)

The header is ``{"alg": "EdDSA", "kid": "<thumbprint>", "typ": "at+jwt"}`` (RFC 9068 media type).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
from mowdeck_contracts.v1 import Principal

from mowdeck_auth.claims import principal_claims
from mowdeck_auth.keys import SIGNING_ALGORITHM, SigningKey
from mowdeck_auth.settings import DEFAULT_AUDIENCE, DEFAULT_ISSUER

ACCESS_TOKEN_TYPE = "at+jwt"  # noqa: S105 - media type, not a secret
"""``typ`` header of MowDeck access tokens."""


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A signed access token and what it says about itself."""

    token: str
    token_id: UUID
    issued_at: datetime
    expires_at: datetime
    kid: str

    @property
    def expires_in(self) -> int:
        """Lifetime in seconds."""
        return int((self.expires_at - self.issued_at).total_seconds())


class TokenIssuer:
    """Signs access tokens with the current key of ``key_provider``."""

    def __init__(
        self,
        key_provider: Callable[[], SigningKey],
        *,
        issuer: str = DEFAULT_ISSUER,
        audience: str = DEFAULT_AUDIENCE,
        ttl: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if ttl <= timedelta(0):
            msg = "ttl must be positive"
            raise ValueError(msg)
        self.key_provider = key_provider
        self.issuer = issuer
        self.audience = audience
        self.ttl = ttl
        self._clock = clock

    def issue(
        self,
        principal: Principal,
        *,
        ttl: timedelta | None = None,
        extra_claims: Mapping[str, Any] | None = None,
    ) -> IssuedToken:
        """Sign a token for ``principal``. A missing ``token_id`` gets a fresh ``jti``."""
        key = self.key_provider()
        now = self._clock()
        expires_at = now + (ttl or self.ttl)
        token_id = principal.token_id or uuid4()
        claims: dict[str, Any] = {
            **principal_claims(principal),
            **(extra_claims or {}),
            "jti": str(token_id),
            "iss": self.issuer,
            "aud": self.audience,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        token = jwt.encode(
            claims,
            key.private_key,
            algorithm=SIGNING_ALGORITHM,
            headers={"kid": key.kid, "typ": ACCESS_TOKEN_TYPE},
        )
        return IssuedToken(token, token_id, now, expires_at, key.kid)
