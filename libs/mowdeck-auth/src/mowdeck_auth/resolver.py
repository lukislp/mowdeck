# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""The kit's ``PrincipalResolver`` for bearer access tokens.

::

    from mowdeck_auth import principal_resolver_from_env

    app = create_app(settings, DESCRIPTOR, principal_resolver=principal_resolver_from_env())

Every failure answers **401** with ``WWW-Authenticate: Bearer error="invalid_token"`` (or plain
``Bearer`` when no credentials were sent, RFC 6750) and the same generic detail; the actual
reason is logged, never returned.

The resolver is :class:`~mowdeck_service_kit.tenancy.BusAware`: the kit attaches it to the event
bus at start-up and it keeps the verifier's deny list of deleted users and erased accounts
current (:mod:`mowdeck_auth.revocation`, ADR 0082). A service writes no code for that. When the
bus refuses the subscription the service still starts (the list fails open); the readiness
check ``token-deny-list`` reports it as degraded until a retry succeeds.
"""

import logging
from collections.abc import Mapping, Sequence

from fastapi import FastAPI, Request
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit.bus import EventBus, Subscription
from mowdeck_service_kit.errors import Unauthorized
from mowdeck_service_kit.health import ReadinessCheck

from mowdeck_auth.revocation import RevocationFeed
from mowdeck_auth.settings import AuthSettings
from mowdeck_auth.verifier import JwtVerifier, TokenError

_log = logging.getLogger("mowdeck_auth.resolver")

INVALID_TOKEN_DETAIL = "invalid or expired access token"  # noqa: S105 - message
MISSING_TOKEN_DETAIL = "an access token is required"  # noqa: S105 - message


def bearer_token(request: Request) -> str | None:
    """The bearer value of the ``Authorization`` header, or None."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


class JwtPrincipalResolver:
    """Resolves the principal from ``Authorization: Bearer <jwt>``."""

    def __init__(self, verifier: JwtVerifier) -> None:
        self.verifier = verifier
        self.revocation_feed = RevocationFeed(verifier.revocations)
        """Feeds the verifier's deny list from the bus, failing open (ADR 0082)."""

    @property
    def revocation_events(self) -> Subscription | None:
        """The subscription that feeds the verifier's deny list, once it is established."""
        return self.revocation_feed.subscription

    async def attach(self, bus: EventBus) -> None:
        """Keep the verifier's deny list current from ``bus`` (called by the kit's
        ``create_app`` when the bus is connected). A refused subscription does not stop the
        service: :class:`~mowdeck_auth.revocation.RevocationFeed` logs it and retries."""
        await self.revocation_feed.start(bus)

    async def detach(self) -> None:
        """Stop feeding the deny list (called by the kit on shutdown)."""
        await self.revocation_feed.stop()

    def readiness_checks(self) -> Sequence[ReadinessCheck]:
        """``token-deny-list``, reported as degraded while the list is not fed."""
        return (self.revocation_feed.readiness_check(),)

    async def resolve(self, request: Request) -> Principal:
        """The verified principal, or :class:`Unauthorized` (401)."""
        token = bearer_token(request)
        if token is None:
            raise Unauthorized(MISSING_TOKEN_DETAIL)
        try:
            return await self.verifier.verify(token)
        except TokenError as exc:
            _log.warning(
                "access token rejected", extra={"reason": exc.reason, "path": request.url.path}
            )
            raise Unauthorized(
                INVALID_TOKEN_DETAIL, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}
            ) from None


def principal_resolver_from_env(
    environ: Mapping[str, str] | None = None,
) -> JwtPrincipalResolver | None:
    """A resolver from ``MOWDECK_AUTH_*`` settings, or None when no JWKS URL is configured."""
    settings = AuthSettings.from_env(environ)
    if settings is None:
        return None
    return JwtPrincipalResolver(JwtVerifier.from_settings(settings))


def install_auth(app: FastAPI, verifier: JwtVerifier) -> JwtPrincipalResolver:
    """Make ``app`` resolve principals from bearer tokens verified by ``verifier``."""
    resolver = JwtPrincipalResolver(verifier)
    app.state.principal_resolver = resolver
    return resolver
