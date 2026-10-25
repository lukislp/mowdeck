"""A fake Apple for the Playwright suite (ADR 0052): the Sign in with Apple endpoints MowDeck uses.

Test stacks only (infra/compose/docker-compose.e2e.yml runs it with the identity image, which
brings PyJWT and cryptography). Traefik routes ``/fake-apple`` to it, so the browser reaches the
authorization page at the stack's own address; Identity calls the token and key endpoints inside
the network.

At startup it generates the stack's keys, so the repository holds no private key:

* the Sign in with Apple key (EC P-256) is written as ``sign_in_key.p8`` into ``FAKE_APPLE_KEY_DIR``
  (a volume Identity mounts read-only, as it mounts the real file from ``secrets/apple``); the
  fake verifies the client secret JWTs with its public half;
* an RSA key signs the ID tokens and is published at ``/fake-apple/auth/keys``.

Endpoints:

* ``GET  /fake-apple/auth/authorize``: checks Services ID, callback, ``response_mode=query``, no
  scope, nonce and state, and shows a form where the test picks the Apple account (``sub``) or
  cancels.
* ``POST /fake-apple/auth/authorize``: back to the callback with ``code`` and ``state`` (or
  ``error=user_cancelled_authorize``), through a page like the fake GitHub.
* ``POST /fake-apple/auth/token``: the client secret (ES256 JWT of the team, audience
  ``https://appleid.apple.com``, subject the Services ID, key id), a single-use code bound to the
  callback and the PKCE verifier; answers with an RS256 ID token.
* ``GET  /fake-apple/auth/keys``: the JWKS.
"""

import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

PREFIX = "/fake-apple"
CALLBACK_PATH = "/api/v1/identity/oauth/apple/callback"
ISSUER = "https://appleid.apple.com"
SERVICES_ID = os.environ.get("FAKE_APPLE_SERVICES_ID", "app.mowdeck.e2e")
TEAM_ID = os.environ.get("FAKE_APPLE_TEAM_ID", "E2ETEAM123")
KEY_ID = os.environ.get("FAKE_APPLE_KEY_ID", "E2EKEY1234")
KEY_DIR = Path(os.environ.get("FAKE_APPLE_KEY_DIR", "/keys"))
KID = "e2e-apple-signing-key"

_lock = threading.Lock()
_codes: dict[str, dict[str, str]] = {}

_client_key = ec.generate_private_key(ec.SECP256R1())
_signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write_client_key() -> None:
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    pem = _client_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    target = KEY_DIR / "sign_in_key.p8"
    partial = KEY_DIR / ".sign_in_key.p8.partial"
    partial.write_bytes(pem)
    partial.chmod(0o644)  # a test key; Identity runs as another user
    partial.replace(target)  # the healthcheck waits for the final name


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _one(values: dict[str, list[str]], key: str) -> str:
    return values.get(key, [""])[0]


def _jwks() -> dict[str, object]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_signing_key.public_key()))
    return {"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]}


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fake Apple: sign in</title></head>
<body>
<h1>Sign in to MowDeck with Apple (fake)</h1>
<form method="post" action="{action}">
<input type="hidden" name="state" value="{state}">
<input type="hidden" name="nonce" value="{nonce}">
<input type="hidden" name="redirect_uri" value="{redirect_uri}">
<input type="hidden" name="code_challenge" value="{challenge}">
<label>Apple account (sub) <input name="sub" data-testid="fake-apple-sub"></label>
<button name="decision" value="allow" data-testid="fake-apple-authorize">Continue</button>
<button name="decision" value="deny" data-testid="fake-apple-cancel">Cancel</button>
</form>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-apple/1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"fake-apple: {format % args}", flush=True)

    def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _redirect(self, url: str) -> None:
        """Back to the callback through a page (no request data in a response header)."""
        target = html.escape(url, quote=True)
        page = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta http-equiv="refresh" content="0;url={target}">'
            f'<title>Redirecting</title></head><body><a href="{target}">Continue</a></body></html>'
        )
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def _form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        return parse_qs(self.rfile.read(length).decode("utf-8"))

    def _path(self) -> tuple[str, dict[str, list[str]]]:
        parts = urlsplit(self.path)
        return parts.path, parse_qs(parts.query)

    def do_GET(self) -> None:
        path, query = self._path()
        if path == f"{PREFIX}/healthz":
            ready = (KEY_DIR / "sign_in_key.p8").exists()
            self._send(200 if ready else 503, b"ok" if ready else b"key not written")
        elif path == f"{PREFIX}/auth/authorize":
            self._authorize_page(query)
        elif path == f"{PREFIX}/auth/keys":
            self._json(200, _jwks())
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path, _ = self._path()
        if path == f"{PREFIX}/auth/authorize":
            self._decide(self._form())
        elif path == f"{PREFIX}/auth/token":
            self._token(self._form())
        else:
            self._json(404, {"error": "not_found"})

    def _authorize_page(self, query: dict[str, list[str]]) -> None:
        redirect_uri = _one(query, "redirect_uri")
        problems = []
        if _one(query, "client_id") != SERVICES_ID:
            problems.append("unknown client_id")
        if not redirect_uri.endswith(CALLBACK_PATH):
            problems.append("redirect_uri is not the registered return URL")
        if _one(query, "response_type") != "code":
            problems.append("response_type must be code")
        if _one(query, "response_mode") != "query":
            problems.append("response_mode must be query (form_post would lose the cookie)")
        if "scope" in query:
            problems.append("no scope expected: neither name nor e-mail is requested")
        if not _one(query, "nonce") or not _one(query, "state"):
            problems.append("state and nonce required")
        if problems:
            self._send(400, "; ".join(problems).encode())
            return
        page = PAGE.format(
            action=f"{PREFIX}/auth/authorize",
            state=html.escape(_one(query, "state")),
            nonce=html.escape(_one(query, "nonce")),
            redirect_uri=html.escape(redirect_uri),
            challenge=html.escape(_one(query, "code_challenge")),
        )
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def _decide(self, form: dict[str, list[str]]) -> None:
        redirect_uri, state = _one(form, "redirect_uri"), _one(form, "state")
        if not redirect_uri.endswith(CALLBACK_PATH):
            self._send(400, b"bad redirect_uri")
            return
        sub = _one(form, "sub").strip()
        if _one(form, "decision") != "allow":
            self._redirect(
                f"{redirect_uri}?{urlencode({'error': 'user_cancelled_authorize', 'state': state})}"
            )
            return
        if not sub:
            self._send(400, b"sub required")
            return
        code = secrets.token_hex(10)
        with _lock:
            _codes[code] = {
                "challenge": _one(form, "code_challenge"),
                "redirect_uri": redirect_uri,
                "nonce": _one(form, "nonce"),
                "sub": sub,
            }
        self._redirect(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}")

    def _secret_ok(self, token: str) -> bool:
        try:
            header = jwt.get_unverified_header(token)
            claims = jwt.decode(
                token,
                _client_key.public_key(),
                algorithms=["ES256"],
                audience=ISSUER,
                issuer=TEAM_ID,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            print(f"fake-apple: bad client secret: {exc}", flush=True)
            return False
        return header.get("kid") == KEY_ID and claims["sub"] == SERVICES_ID

    def _token(self, form: dict[str, list[str]]) -> None:
        if _one(form, "client_id") != SERVICES_ID or not self._secret_ok(
            _one(form, "client_secret")
        ):
            self._json(400, {"error": "invalid_client"})
            return
        if _one(form, "grant_type") != "authorization_code":
            self._json(400, {"error": "unsupported_grant_type"})
            return
        with _lock:
            issued = _codes.pop(_one(form, "code"), None)
        if issued is None or _one(form, "redirect_uri") != issued["redirect_uri"]:
            self._json(400, {"error": "invalid_grant"})
            return
        if issued["challenge"] and _s256(_one(form, "code_verifier")) != issued["challenge"]:
            self._json(400, {"error": "invalid_grant"})
            return
        now = int(time.time())
        id_token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": SERVICES_ID,
                "exp": now + 600,
                "iat": now,
                "sub": issued["sub"],
                "nonce": issued["nonce"],
                "nonce_supported": True,
                # Claims MowDeck must ignore.
                "email": f"{issued['sub']}@privaterelay.appleid.com",
                "email_verified": True,
                "is_private_email": True,
            },
            _signing_key,
            algorithm="RS256",
            headers={"kid": KID},
        )
        self._json(
            200,
            {
                "access_token": "fake-apple-access-" + secrets.token_hex(8),
                "refresh_token": "fake-apple-refresh-" + secrets.token_hex(8),
                "token_type": "Bearer",
                "expires_in": 3600,
                "id_token": id_token,
            },
        )


def main() -> None:
    _write_client_key()
    port = int(os.environ.get("FAKE_APPLE_PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - test container
    print(f"fake-apple: listening on {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
