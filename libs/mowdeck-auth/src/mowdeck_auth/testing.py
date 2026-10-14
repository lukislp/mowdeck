"""Test helpers: keys, minted tokens and a fake JWKS endpoint.

::

    from mowdeck_auth.testing import TestKeys

    keys = TestKeys()
    token = keys.mint(make_principal(permissions=["mower.view"]))
    resolver = keys.resolver()  # JwtPrincipalResolver over a fake JWKS (httpx MockTransport)
    app = create_app(settings, descriptor, principal_resolver=resolver)
    await client.get("/api/v1/x", headers={"Authorization": f"Bearer {token}"})
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from mowdeck_contracts.v1 import Principal

from mowdeck_auth.issuer import TokenIssuer
from mowdeck_auth.jwks import JwksCache
from mowdeck_auth.keys import SigningKey
from mowdeck_auth.resolver import JwtPrincipalResolver
from mowdeck_auth.settings import DEFAULT_AUDIENCE, DEFAULT_ISSUER
from mowdeck_auth.verifier import JwtVerifier

__all__ = ["TEST_JWKS_URL", "FakeJwksServer", "TestKeys", "bearer"]

TEST_JWKS_URL = "http://identity.test/.well-known/jwks.json"


def bearer(token: str) -> dict[str, str]:
    """``{"Authorization": "Bearer <token>"}``."""
    return {"Authorization": f"Bearer {token}"}


class FakeJwksServer:
    """Serves a mutable JWKS through an ``httpx.MockTransport`` and counts the requests."""

    def __init__(self, keys: list[SigningKey]) -> None:
        self.keys = keys
        self.requests = 0
        self.fail = False
        """When True the endpoint answers 503 (to test stale keys and outages)."""

    def document(self) -> dict[str, Any]:
        """The current JWKS document."""
        return {"keys": [key.public_jwk() for key in self.keys]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        """The mock endpoint."""
        self.requests += 1
        if self.fail:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json=self.document())

    @property
    def transport(self) -> httpx.MockTransport:
        """An httpx transport answering every request with :meth:`handler`."""
        return httpx.MockTransport(self.handler)


class TestKeys:
    """A signing key plus everything needed to mint and verify tokens in tests."""

    __test__ = False  # not a pytest test class

    def __init__(
        self,
        *,
        issuer: str = DEFAULT_ISSUER,
        audience: str = DEFAULT_AUDIENCE,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.key = SigningKey.generate()
        self.issuer = issuer
        self.audience = audience
        self.clock = clock
        self.server = FakeJwksServer([self.key])

    def rotate(self) -> SigningKey:
        """Switch to a new signing key; the old one stays published."""
        self.key = SigningKey.generate()
        self.server.keys.append(self.key)
        return self.key

    def mint(
        self,
        principal: Principal,
        *,
        ttl: timedelta = timedelta(minutes=10),
        extra_claims: Mapping[str, Any] | None = None,
        key: SigningKey | None = None,
    ) -> str:
        """A signed access token for ``principal``."""
        signing_key = key or self.key
        issuer = TokenIssuer(
            lambda: signing_key,
            issuer=self.issuer,
            audience=self.audience,
            ttl=ttl,
            clock=self.clock,
        )
        return issuer.issue(principal, extra_claims=extra_claims).token

    def cache(self, **options: Any) -> JwksCache:
        """A :class:`JwksCache` on the fake JWKS endpoint."""
        return JwksCache(TEST_JWKS_URL, transport=self.server.transport, **options)

    def verifier(self, **cache_options: Any) -> JwtVerifier:
        """A verifier using :meth:`cache`."""
        return JwtVerifier(self.cache(**cache_options), issuer=self.issuer, audience=self.audience)

    def resolver(self, **cache_options: Any) -> JwtPrincipalResolver:
        """A principal resolver using :meth:`verifier`."""
        return JwtPrincipalResolver(self.verifier(**cache_options))
