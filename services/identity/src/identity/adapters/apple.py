"""Sign in with Apple as an OAuth 2 / OpenID Connect login provider (ADR 0052), the
``OAuthClient`` port on httpx and PyJWT.

* Authorization: ``https://appleid.apple.com/auth/authorize`` with ``client_id`` (the Services
  ID), ``redirect_uri``, ``response_type=code``, ``response_mode=query``, ``state``, ``nonce``
  and the PKCE ``code_challenge`` (S256). **No scope** is requested: MowDeck wants neither the
  name nor the e-mail address, and without scope Apple answers with a plain redirect (GET), so
  the SameSite=Lax cookie that binds the round trip to the browser comes along (``form_post``,
  which Apple demands once a scope is requested, is a cross-site POST that drops it).
* Token: ``POST https://appleid.apple.com/auth/token`` with the code, the PKCE verifier and
  the client secret, a short-lived ES256 JWT signed with the Sign in with Apple key (.p8).
* The ID token is validated here: signature against Apple's JWKS (cached, refreshed on an unknown
  ``kid``), ``iss``, ``aud`` (the Services ID), ``exp``/``iat`` and the ``nonce``. The account is
  ``sub``: stable per developer team, and the only claim used. The e-mail claims (Apple may add
  ``email``, possibly a private relay address) are never read or stored.
* Apple's access and refresh tokens are dropped unread and are not revoked (as for GitHub,
  ADR 0051 amendment): MowDeck stores no Apple token.

PKCE: Apple's documentation does not list ``code_challenge`` / ``code_verifier`` for this
flow. They are sent anyway (an OIDC server ignores unknown parameters and enforces known
ones); the state, the browser-bound cookie and the nonce hold the flow together whether or not
Apple checks the verifier. ``pkce=False`` leaves them out.
"""

import asyncio
import hmac
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from mowdeck_service_kit import get_logger

from identity.application.ports import ExternalProfile, ProviderRejected, ProviderUnavailable
from identity.domain.credentials import s256_challenge

_log = get_logger("identity.apple")

ISSUER = "https://appleid.apple.com"
AUTHORIZE_URL = f"{ISSUER}/auth/authorize"
TOKEN_URL = f"{ISSUER}/auth/token"
KEYS_URL = f"{ISSUER}/auth/keys"
DISPLAY_NAME = "Apple ID"
"""The label stored and shown for a linked account: Apple tells nothing else (no scope asked)."""

SECRET_LIFETIME = 3600
"""Seconds a client secret JWT is valid (Apple allows up to 180 days; short is safer)."""
SECRET_RENEW_BEFORE = 300
"""A cached client secret is replaced when it has less than this left."""
KEYS_MAX_AGE = 24 * 3600
"""Apple's signing keys are re-read at least daily."""
KEYS_MIN_INTERVAL = 60
"""...and on an unknown ``kid`` at most once a minute (a forged token must not make MowDeck
hammer Apple)."""
LEEWAY = 30
ID_TOKEN_ALGORITHMS = ["RS256", "ES256"]
MAX_SUBJECT_LENGTH = 255


def load_signing_key(pem: str) -> ec.EllipticCurvePrivateKey:
    """Parse the .p8 file (PKCS#8 PEM, an EC P-256 key). Raises ``ValueError`` when it is not."""
    try:
        key = load_pem_private_key(pem.strip().encode(), password=None)
    except (ValueError, TypeError) as exc:
        msg = "the private key is not a readable unencrypted PEM key"
        raise ValueError(msg) from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        msg = "the private key is not an EC P-256 key (Sign in with Apple keys are)"
        raise ValueError(msg)
    return key


class AppleOAuthClient:
    """The Apple endpoints of the authorization code flow."""

    provider = "apple"

    def __init__(
        self,
        *,
        services_id: str,
        team_id: str,
        key_id: str,
        private_key: ec.EllipticCurvePrivateKey,
        authorize_url: str = AUTHORIZE_URL,
        token_url: str = TOKEN_URL,
        keys_url: str = KEYS_URL,
        issuer: str = ISSUER,
        pkce: bool = True,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.services_id = services_id
        self.team_id = team_id
        self.key_id = key_id
        self._key = private_key
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.keys_url = keys_url
        self.issuer = issuer
        self.pkce = pkce
        self.timeout = timeout
        self._transport = transport
        """Tests pass ``httpx.MockTransport``; None uses the network."""
        self._clock = clock
        self._secret: tuple[str, float] | None = None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._keys_read_at = 0.0
        self._keys_lock = asyncio.Lock()

    # --- authorization -------------------------------------------------------------------

    @staticmethod
    def nonce_for(code_challenge: str) -> str:
        """The ID token's nonce of a round trip: the PKCE S256 challenge, which the browser's
        verifier reproduces (so the client needs no state of its own). It is in the
        authorization URL, so it is no secret; it ties the token to this round trip."""
        return code_challenge

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        """The Apple authorization page for this round trip (no scope, see the module doc)."""
        params = {
            "client_id": self.services_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "response_mode": "query",
            "state": state,
            "nonce": self.nonce_for(code_challenge),
        }
        if self.pkce:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return f"{self.authorize_url}?{urlencode(params)}"

    # --- client secret -------------------------------------------------------------------

    def client_secret(self) -> str:
        """The ES256 JWT that serves as the client secret, cached until shortly before it ends."""
        now = self._clock()
        if self._secret is not None and self._secret[1] - now > SECRET_RENEW_BEFORE:
            return self._secret[0]
        expires = now + SECRET_LIFETIME
        token = jwt.encode(
            {
                "iss": self.team_id,
                "iat": int(now),
                "exp": int(expires),
                "aud": ISSUER,
                "sub": self.services_id,
            },
            self._key,
            algorithm="ES256",
            headers={"kid": self.key_id},
        )
        self._secret = (token, expires)
        return token

    # --- code to profile -----------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=False,
            headers={"User-Agent": "MowDeck-Identity"},
        )

    async def profile(self, *, code: str, code_verifier: str, redirect_uri: str) -> ExternalProfile:
        """Redeem the code and read the account's ``sub`` from the validated ID token."""
        nonce = self.nonce_for(s256_challenge(code_verifier))
        async with self._client() as http:
            id_token = await self._token(http, code, code_verifier, redirect_uri)
            claims = await self._validate(http, id_token, nonce)
        return ExternalProfile(subject=claims["sub"], display_name=DISPLAY_NAME)

    async def _token(
        self, http: httpx.AsyncClient, code: str, verifier: str, redirect_uri: str
    ) -> str:
        form = {
            "client_id": self.services_id,
            "client_secret": self.client_secret(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if self.pkce:
            form["code_verifier"] = verifier
        try:
            response = await http.post(self.token_url, data=form)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"token endpoint: {type(exc).__name__}") from exc
        body = _json(response)
        if response.status_code >= 500:
            raise ProviderUnavailable(f"token endpoint answered {response.status_code}")
        error = body.get("error") if isinstance(body, dict) else None
        if error in ("invalid_client", "unauthorized_client", "unsupported_grant_type"):
            # The installation's settings (Services ID, team, key) are wrong, not the attempt.
            raise ProviderUnavailable(f"token endpoint: {error}")
        id_token = body.get("id_token") if isinstance(body, dict) else None
        if response.status_code != 200 or error or not isinstance(id_token, str) or not id_token:
            raise ProviderRejected(f"token endpoint: {error or response.status_code}")
        return id_token

    # --- ID token ------------------------------------------------------------------------

    async def _validate(self, http: httpx.AsyncClient, id_token: str, nonce: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise ProviderRejected("id_token: not a JWT") from exc
        kid = header.get("kid")
        if not isinstance(kid, str) or header.get("alg") not in ID_TOKEN_ALGORITHMS:
            raise ProviderRejected("id_token: unexpected header")
        key = await self._key_for(http, kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key.key,
                algorithms=ID_TOKEN_ALGORITHMS,
                audience=self.services_id,
                issuer=self.issuer,
                leeway=LEEWAY,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise ProviderRejected(f"id_token: {type(exc).__name__}") from exc
        given = claims.get("nonce")
        if not isinstance(given, str) or not hmac.compare_digest(given.encode(), nonce.encode()):
            raise ProviderRejected("id_token: wrong nonce")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > MAX_SUBJECT_LENGTH:
            raise ProviderRejected("id_token: no usable sub")
        return claims

    async def _key_for(self, http: httpx.AsyncClient, kid: str) -> jwt.PyJWK:
        """Apple's key ``kid``; the cache is re-read when it is old or the key is unknown."""
        known = self._keys.get(kid)
        if known is not None and self._clock() - self._keys_read_at < KEYS_MAX_AGE:
            return known
        async with self._keys_lock:
            now = self._clock()
            known = self._keys.get(kid)
            if known is not None and now - self._keys_read_at < KEYS_MAX_AGE:
                return known
            if known is None and self._keys and now - self._keys_read_at < KEYS_MIN_INTERVAL:
                raise ProviderRejected("id_token: unknown signing key")
            await self._read_keys(http)
        found = self._keys.get(kid)
        if found is None:
            raise ProviderRejected("id_token: unknown signing key")
        return found

    async def _read_keys(self, http: httpx.AsyncClient) -> None:
        try:
            response = await http.get(self.keys_url)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"keys endpoint: {type(exc).__name__}") from exc
        body = _json(response)
        if response.status_code != 200 or not isinstance(body, dict):
            raise ProviderUnavailable(f"keys endpoint answered {response.status_code}")
        keys: dict[str, jwt.PyJWK] = {}
        entries = body.get("keys")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("kid"), str):
                continue
            try:
                keys[entry["kid"]] = jwt.PyJWK.from_dict(entry)
            except jwt.PyJWTError:
                _log.warning("ignored an unusable Apple signing key", kid=entry["kid"])
        if not keys:
            raise ProviderUnavailable("keys endpoint: no usable keys")
        self._keys = keys
        self._keys_read_at = self._clock()


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
