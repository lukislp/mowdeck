"""MowDeck access tokens: EdDSA JWTs issued by Identity and verified by every service.

* :class:`JwtVerifier` checks a token against the public keys of a :class:`KeySource`
  (:class:`JwksCache` over HTTP, or :class:`StaticKeySource`).
* :class:`JwtPrincipalResolver` plugs the verifier into the service kit (401 on any failure).
* :class:`TokenIssuer` signs tokens with a :class:`SigningKey` (Identity and tests).
* ``mowdeck_auth.testing`` has keys, minted tokens and a fake JWKS endpoint for tests.
"""

from mowdeck_auth.claims import REQUIRED_CLAIMS, principal_claims, principal_from_claims
from mowdeck_auth.issuer import ACCESS_TOKEN_TYPE, IssuedToken, TokenIssuer
from mowdeck_auth.jwks import JwksCache, KeySource, StaticKeySource, parse_jwks
from mowdeck_auth.keys import (
    ALLOWED_ALGORITHMS,
    SIGNING_ALGORITHM,
    SigningKey,
    public_jwk,
    thumbprint,
)
from mowdeck_auth.resolver import (
    JwtPrincipalResolver,
    bearer_token,
    install_auth,
    principal_resolver_from_env,
)
from mowdeck_auth.settings import DEFAULT_AUDIENCE, DEFAULT_ISSUER, AuthSettings
from mowdeck_auth.step_up import (
    ReauthRequired,
    StepUpUnavailable,
    ensure_recent_auth,
    require_recent_auth,
    step_up_max_age,
)
from mowdeck_auth.verifier import JwtVerifier, TokenError, VerifiedToken

__version__ = "0.1.0"

__all__ = [
    "ACCESS_TOKEN_TYPE",
    "ALLOWED_ALGORITHMS",
    "DEFAULT_AUDIENCE",
    "DEFAULT_ISSUER",
    "REQUIRED_CLAIMS",
    "SIGNING_ALGORITHM",
    "AuthSettings",
    "IssuedToken",
    "JwksCache",
    "JwtPrincipalResolver",
    "JwtVerifier",
    "KeySource",
    "ReauthRequired",
    "SigningKey",
    "StaticKeySource",
    "StepUpUnavailable",
    "TokenError",
    "TokenIssuer",
    "VerifiedToken",
    "bearer_token",
    "ensure_recent_auth",
    "install_auth",
    "parse_jwks",
    "principal_claims",
    "principal_from_claims",
    "principal_resolver_from_env",
    "public_jwk",
    "require_recent_auth",
    "step_up_max_age",
    "thumbprint",
]
