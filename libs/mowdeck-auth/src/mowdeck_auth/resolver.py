"""The kit's ``PrincipalResolver`` for bearer access tokens.

::

    from mowdeck_auth import principal_resolver_from_env

    app = create_app(settings, DESCRIPTOR, principal_resolver=principal_resolver_from_env())

Every failure answers **401** with ``WWW-Authenticate: Bearer error="invalid_token"`` (or plain
``Bearer`` when no credentials were sent, RFC 6750) and the same generic detail; the actual
reason is logged, never returned.
"""

import logging
from collections.abc import Mapping

from fastapi import FastAPI, Request
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit.errors import Unauthorized

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
