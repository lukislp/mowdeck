"""Token verification with ``mowdeck-auth`` (the same JWKS, issuer and audience as HTTP)."""

from collections.abc import Mapping

from mowdeck_auth import AuthSettings, JwtVerifier, TokenError
from mowdeck_service_kit import get_logger

from realtime.domain.errors import InvalidTokenError
from realtime.domain.models import Authenticated

_log = get_logger("realtime.auth")


class JwtTokenVerifier:
    """The :class:`~realtime.application.ports.TokenVerifier` over a :class:`JwtVerifier`.

    The kit's principal resolvers work on HTTP requests; a WebSocket carries the token in its
    first message, so Realtime calls the verifier directly.
    """

    def __init__(self, verifier: JwtVerifier) -> None:
        self.verifier = verifier

    async def verify(self, token: str) -> Authenticated:
        """The principal and expiry, or :class:`InvalidTokenError` (reason logged only)."""
        try:
            verified = await self.verifier.verify_token(token)
        except TokenError as exc:
            _log.warning("access token rejected", reason=exc.reason)
            raise InvalidTokenError("invalid or expired access token") from None
        return Authenticated(principal=verified.principal, expires_at=verified.expires_at)


class RejectAllTokens:
    """Used when ``MOWDECK_AUTH_JWKS_URL`` is not set: every socket closes with 4401."""

    async def verify(self, token: str) -> Authenticated:
        """Always :class:`InvalidTokenError`."""
        raise InvalidTokenError("token verification is not configured")


def verifier_from_env(environ: Mapping[str, str] | None = None) -> JwtTokenVerifier | None:
    """A verifier from the ``MOWDECK_AUTH_*`` settings, None without a JWKS URL."""
    settings = AuthSettings.from_env(environ)
    if settings is None:
        return None
    return JwtTokenVerifier(JwtVerifier.from_settings(settings))
