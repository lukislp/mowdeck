"""A fake GitHub for the Playwright suite (ADR 0051): the OAuth App endpoints MowDeck uses.

Test stacks only (infra/compose/docker-compose.e2e.yml runs it with the identity image; standard
library only). Traefik routes ``/fake-github`` to it, so the browser reaches the authorization
page at the stack's own address; Identity calls the token and API endpoints inside the network.

* ``GET  /fake-github/login/oauth/authorize``: checks client id, callback, scope and the S256
  challenge, and shows a form where the test picks the GitHub account (id and login) or cancels.
* ``POST /fake-github/login/oauth/authorize``: redirects to the callback with ``code`` and
  ``state`` (or ``error=access_denied``).
* ``POST /fake-github/login/oauth/access_token``: client id and secret, single-use code bound to
  the callback, PKCE verifier; errors with status 200 and an ``error`` field, like GitHub.
* ``GET  /fake-github/api/user``: the token's account (with an e-mail address MowDeck ignores).

Anything else is a 404, token revocation included: MowDeck keeps the user's authorization at
GitHub and never revokes the token it used for its one request (ADR 0051, amendment 2026-10-25).
"""

import base64
import hashlib
import html
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

PREFIX = "/fake-github"
CALLBACK_PATH = "/api/v1/identity/oauth/github/callback"
CLIENT_ID = os.environ.get("FAKE_GITHUB_CLIENT_ID", "e2e-github-client")
CLIENT_SECRET = (
    Path(os.environ["FAKE_GITHUB_CLIENT_SECRET_FILE"]).read_text("utf-8").strip()
    if os.environ.get("FAKE_GITHUB_CLIENT_SECRET_FILE")
    else "e2e-github-secret"
)

_lock = threading.Lock()
_codes: dict[str, dict[str, object]] = {}
_tokens: dict[str, dict[str, object]] = {}


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _one(values: dict[str, list[str]], key: str) -> str:
    return values.get(key, [""])[0]


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fake GitHub: authorize</title></head>
<body>
<h1>Authorize MowDeck (fake GitHub)</h1>
<form method="post" action="{action}">
<input type="hidden" name="state" value="{state}">
<input type="hidden" name="redirect_uri" value="{redirect_uri}">
<input type="hidden" name="code_challenge" value="{challenge}">
<label>GitHub user id
<input name="user_id" data-testid="fake-github-id" inputmode="numeric"></label>
<label>GitHub login <input name="login" data-testid="fake-github-login"></label>
<button name="decision" value="allow" data-testid="fake-github-authorize">Authorize</button>
<button name="decision" value="deny" data-testid="fake-github-cancel">Cancel</button>
</form>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-github/1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"fake-github: {format % args}", flush=True)

    # --- helpers ---------------------------------------------------------------------------------

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

    # --- endpoints -------------------------------------------------------------------------------

    def do_GET(self) -> None:
        path, query = self._path()
        if path == f"{PREFIX}/healthz":
            self._send(200, b"ok")
        elif path == f"{PREFIX}/login/oauth/authorize":
            self._authorize_page(query)
        elif path == f"{PREFIX}/api/user":
            self._user()
        else:
            self._json(404, {"message": "Not Found"})

    def do_POST(self) -> None:
        path, _ = self._path()
        if path == f"{PREFIX}/login/oauth/authorize":
            self._decide(self._form())
        elif path == f"{PREFIX}/login/oauth/access_token":
            self._token(self._form())
        else:
            self._json(404, {"message": "Not Found"})

    def do_DELETE(self) -> None:
        self._json(404, {"message": "Not Found"})

    def _authorize_page(self, query: dict[str, list[str]]) -> None:
        redirect_uri = _one(query, "redirect_uri")
        problems = []
        if _one(query, "client_id") != CLIENT_ID:
            problems.append("unknown client_id")
        if not redirect_uri.endswith(CALLBACK_PATH):
            problems.append("redirect_uri is not the registered callback")
        if _one(query, "scope") != "read:user":
            problems.append("unexpected scope")
        if _one(query, "code_challenge_method") != "S256" or not _one(query, "code_challenge"):
            problems.append("PKCE S256 required")
        if not _one(query, "state"):
            problems.append("state required")
        if problems:
            self._send(400, "; ".join(problems).encode())
            return
        page = PAGE.format(
            action=f"{PREFIX}/login/oauth/authorize",
            state=html.escape(_one(query, "state")),
            redirect_uri=html.escape(redirect_uri),
            challenge=html.escape(_one(query, "code_challenge")),
        )
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def _decide(self, form: dict[str, list[str]]) -> None:
        redirect_uri, state = _one(form, "redirect_uri"), _one(form, "state")
        if not redirect_uri.endswith(CALLBACK_PATH):
            self._send(400, b"bad redirect_uri")
            return
        if _one(form, "decision") != "allow":
            query = {"error": "access_denied", "error_description": "denied", "state": state}
            self._redirect(f"{redirect_uri}?{urlencode(query)}")
            return
        try:
            user_id = int(_one(form, "user_id"))
        except ValueError:
            self._send(400, b"user_id must be a number")
            return
        code = secrets.token_hex(10)
        with _lock:
            _codes[code] = {
                "challenge": _one(form, "code_challenge"),
                "redirect_uri": redirect_uri,
                "user": {"id": user_id, "login": _one(form, "login") or f"user{user_id}"},
            }
        self._redirect(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}")

    def _token(self, form: dict[str, list[str]]) -> None:
        if _one(form, "client_id") != CLIENT_ID or _one(form, "client_secret") != CLIENT_SECRET:
            self._json(200, {"error": "incorrect_client_credentials"})
            return
        with _lock:
            issued = _codes.pop(_one(form, "code"), None)
        if issued is None:
            self._json(200, {"error": "bad_verification_code"})
            return
        if _one(form, "redirect_uri") != issued["redirect_uri"]:
            self._json(200, {"error": "redirect_uri_mismatch"})
            return
        if _s256(_one(form, "code_verifier")) != issued["challenge"]:
            self._json(200, {"error": "invalid_grant"})
            return
        token = "gho_" + secrets.token_hex(16)
        with _lock:
            _tokens[token] = issued["user"]  # type: ignore[assignment]
        self._json(200, {"access_token": token, "token_type": "bearer", "scope": "read:user"})

    def _user(self) -> None:
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        with _lock:
            user = _tokens.get(token)
        if user is None:
            self._json(401, {"message": "Bad credentials"})
            return
        login = user["login"]
        self._json(200, {**user, "email": f"{login}@users.example", "name": login})


def main() -> None:
    port = int(os.environ.get("FAKE_GITHUB_PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - test container
    print(f"fake-github: listening on {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
