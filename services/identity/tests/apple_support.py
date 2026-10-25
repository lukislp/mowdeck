"""A fake Apple for the identity tests (ADR 0052): authorize check, token endpoint and the key
set behind an ``httpx.MockTransport``, strict like the real one.

* ``authorize(url, sub)`` plays the user approving at appleid.apple.com: it checks the
  authorization URL (Services ID, registered callback, ``response_mode=query``, *no scope*,
  nonce, S256 challenge) and returns the callback's query (``state``, ``code``).
* The token endpoint verifies the client secret as Apple does (an ES256 JWT of the team, signed
  with the registered key, audience ``https://appleid.apple.com``, subject the Services ID),
  the code (single use, bound to the redirect URI) and the PKCE verifier, and answers with an
  RS256 ID token (with an e-mail claim, which MowDeck must ignore).
* ``claims`` overrides ID token claims (``None`` removes one), ``kid`` and ``signing_key`` forge
  the signature; ``rotate()`` publishes a new signing key.
"""

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

SERVICES_ID = "app.mowdeck.signin"
TEAM_ID = "ABCDE12345"
KEY_ID = "KEY1234567"
ORIGIN = "https://mowdeck.example.org"
CALLBACK = f"{ORIGIN}/api/v1/identity/oauth/apple/callback"
AUTHORIZE = "https://apple.test/auth/authorize"
TOKEN = "https://apple.test/auth/token"  # noqa: S105 - a URL
KEYS = "https://apple.test/auth/keys"
ISSUER = "https://appleid.apple.com"


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def new_ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


_RSA_KEYS: list[rsa.RSAPrivateKey] = []


def rsa_key(index: int = 0) -> rsa.RSAPrivateKey:
    """Generated once per run (RSA keys are slow to make)."""
    while len(_RSA_KEYS) <= index:
        _RSA_KEYS.append(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    return _RSA_KEYS[index]


def write_key(directory: Path, key: ec.EllipticCurvePrivateKey) -> Path:
    path = directory / "sign_in_key.p8"
    path.write_text(pem(key), encoding="utf-8")
    return path


def settings(key_file: Path) -> dict[str, Any]:
    """Settings that turn Apple login on against this fake."""
    return {
        "identity_public_url": ORIGIN,
        "identity_apple_services_id": SERVICES_ID,
        "identity_apple_team_id": TEAM_ID,
        "identity_apple_key_id": KEY_ID,
        "identity_apple_private_key_file": str(key_file),
        "identity_apple_authorize_url": AUTHORIZE,
        "identity_apple_token_url": TOKEN,
        "identity_apple_keys_url": KEYS,
    }


@dataclass
class FakeApple:
    """The fake; ``fail`` makes an endpoint misbehave (``token_5xx``, ``keys_5xx``, ``network``,
    ``no_id_token``, ``invalid_client``)."""

    client_key: ec.EllipticCurvePrivateKey = field(default_factory=new_ec_key)
    signing_index: int = 0
    kid: str = "AIDOPK1"
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    secrets_seen: list[dict[str, Any]] = field(default_factory=list)
    form_seen: list[dict[str, str]] = field(default_factory=list)
    fail: set[str] = field(default_factory=set)
    claims: dict[str, Any] = field(default_factory=dict)
    forged_kid: str | None = None
    forged_key: rsa.RSAPrivateKey | None = None
    pkce: bool = True
    published: list[tuple[str, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.published = [(self.kid, self.signing_index)]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def rotate(self) -> None:
        """Apple signs with a new key from now on; the key set lists the new one only."""
        self.signing_index += 1
        self.kid = f"NEW{self.signing_index}"
        self.published = [(self.kid, self.signing_index)]

    def authorize(self, url: str, sub: str) -> dict[str, str]:
        """Approve the authorization request ``url`` as the Apple account ``sub``."""
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZE
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert query["client_id"] == SERVICES_ID
        assert query["redirect_uri"] == CALLBACK
        assert query["response_type"] == "code"
        assert query["response_mode"] == "query"  # form_post would lose the Lax cookie
        assert "scope" not in query  # neither name nor e-mail
        assert len(query["state"]) >= 43 and len(query["nonce"]) >= 43
        if self.pkce:
            assert query["code_challenge_method"] == "S256"
        code = secrets.token_hex(10)
        self.codes[code] = {
            "challenge": query.get("code_challenge"),
            "redirect_uri": query["redirect_uri"],
            "nonce": query["nonce"],
            "sub": sub,
        }
        return {"state": query["state"], "code": code}

    def cancel(self, url: str) -> dict[str, str]:
        """The user pressed "Cancel" at appleid.apple.com."""
        state = parse_qs(urlsplit(url).query)["state"][0]
        return {"state": state, "error": "user_cancelled_authorize"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        self.requests.append(f"{request.method} {url}")
        if "network" in self.fail:
            raise httpx.ConnectError("down", request=request)
        if request.method == "POST" and url == TOKEN:
            return self._token(request)
        if request.method == "GET" and url == KEYS:
            if "keys_5xx" in self.fail:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, json={"keys": [self._jwk(k, i) for k, i in self.published]})
        return httpx.Response(404, json={"error": "not_found"})

    @staticmethod
    def _jwk(kid: str, index: int) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key(index).public_key()))
        return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}

    def _token(self, request: httpx.Request) -> httpx.Response:
        if "token_5xx" in self.fail:
            return httpx.Response(502, text="bad gateway")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.form_seen.append(form)
        if "invalid_client" in self.fail or not self._secret_ok(form.get("client_secret", "")):
            return httpx.Response(400, json={"error": "invalid_client"})
        if form.get("client_id") != SERVICES_ID or form.get("grant_type") != "authorization_code":
            return httpx.Response(400, json={"error": "invalid_request"})
        issued = self.codes.pop(form.get("code", ""), None)
        if issued is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if form.get("redirect_uri") != issued["redirect_uri"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if issued["challenge"] and s256(form.get("code_verifier", "")) != issued["challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if "no_id_token" in self.fail:
            return httpx.Response(200, json={"access_token": "a", "token_type": "Bearer"})
        return httpx.Response(
            200,
            json={
                "access_token": "apple-access-" + secrets.token_hex(8),
                "refresh_token": "apple-refresh-" + secrets.token_hex(8),
                "token_type": "Bearer",
                "expires_in": 3600,
                "id_token": self.id_token(issued["sub"], issued["nonce"]),
            },
        )

    def _secret_ok(self, token: str) -> bool:
        try:
            header = jwt.get_unverified_header(token)
            claims = jwt.decode(
                token,
                self.client_key.public_key(),
                algorithms=["ES256"],
                audience=ISSUER,
                issuer=TEAM_ID,
                # The tests move MowDeck's clock around; Apple's time is the real one.
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError:
            return False
        self.secrets_seen.append({"header": header, "claims": claims})
        return header.get("kid") == KEY_ID and claims["sub"] == SERVICES_ID

    def id_token(self, sub: str, nonce: str) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": SERVICES_ID,
            "exp": now + 600,
            "iat": now,
            "sub": sub,
            "nonce": nonce,
            "nonce_supported": True,
            "c_hash": "x",
            "email": "relay@privaterelay.appleid.com",
            "email_verified": True,
            "is_private_email": True,
        }
        for name, value in self.claims.items():
            if value is None:
                claims.pop(name, None)
            else:
                claims[name] = value
        key = self.forged_key or rsa_key(self.signing_index)
        return jwt.encode(
            claims, key, algorithm="RS256", headers={"kid": self.forged_kid or self.kid}
        )
