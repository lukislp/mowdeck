"""A fake GitHub for the identity tests (ADR 0051): the OAuth endpoints and ``GET /user`` behind
an ``httpx.MockTransport``, strict like the real one.

* ``authorize(url, account)`` plays the user approving on github.com: it checks the
  authorization URL (client id, registered callback, scope, S256 challenge) and returns the
  callback's query (``state``, ``code``).
* The token endpoint checks client id and secret, the code (single use, bound to the redirect
  URI) and the PKCE verifier against the challenge, and answers errors with status 200 and an
  ``error`` field, as GitHub does.
* ``GET /user`` answers for the token's account (with an e-mail address, which MowDeck must
  ignore); ``DELETE /applications/{client_id}/token`` revokes the token.
"""

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

CLIENT_ID = "Iv1.mowdecktest"
CLIENT_SECRET = "github-test-secret"  # noqa: S105 - test value
ORIGIN = "https://mowdeck.example.org"
CALLBACK = f"{ORIGIN}/api/v1/identity/oauth/github/callback"
AUTHORIZE = "https://github.test/login/oauth/authorize"
TOKEN = "https://github.test/login/oauth/access_token"  # noqa: S105 - a URL
API_URL = "https://api.github.test"

SETTINGS: dict[str, Any] = {
    "identity_public_url": ORIGIN,
    "identity_github_client_id": CLIENT_ID,
    "identity_github_client_secret": CLIENT_SECRET,
    "identity_github_authorize_url": AUTHORIZE,
    "identity_github_token_url": TOKEN,
    "identity_github_api_url": API_URL,
}
"""Settings that turn GitHub login on against this fake."""


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass
class GitHubAccount:
    """A GitHub user."""

    id: int
    login: str
    email: str | None = None

    def json(self) -> dict[str, Any]:
        return {"id": self.id, "login": self.login, "email": self.email, "name": self.login}


@dataclass
class FakeGitHub:
    """The fake; ``fail`` makes an endpoint misbehave (``token_5xx``, ``user_5xx``,
    ``network``, ``bad_user``)."""

    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    tokens: dict[str, GitHubAccount] = field(default_factory=dict)
    revoked: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    fail: set[str] = field(default_factory=set)
    user_body: dict[str, Any] | None = None

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def authorize(self, url: str, account: GitHubAccount) -> dict[str, str]:
        """Approve the authorization request ``url`` as ``account``: the callback query."""
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZE
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert query["client_id"] == CLIENT_ID
        assert query["redirect_uri"] == CALLBACK
        assert query["scope"] == "read:user"
        assert query["code_challenge_method"] == "S256"
        assert query["allow_signup"] == "false"
        assert len(query["state"]) >= 43
        code = secrets.token_hex(10)
        self.codes[code] = {
            "challenge": query["code_challenge"],
            "redirect_uri": query["redirect_uri"],
            "account": account,
        }
        return {"state": query["state"], "code": code}

    def deny(self, url: str) -> dict[str, str]:
        """The user clicked "Cancel" on github.com."""
        state = parse_qs(urlsplit(url).query)["state"][0]
        return {"state": state, "error": "access_denied", "error_description": "denied"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        self.requests.append(f"{request.method} {url}")
        if "network" in self.fail:
            raise httpx.ConnectError("down", request=request)
        if request.method == "POST" and url == TOKEN:
            return self._token(request)
        if request.method == "GET" and url == f"{API_URL}/user":
            return self._user(request)
        if request.method == "DELETE" and url == f"{API_URL}/applications/{CLIENT_ID}/token":
            token = json.loads(request.content)["access_token"]
            self.revoked.append(token)
            self.tokens.pop(token, None)
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "Not Found"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        if "token_5xx" in self.fail:
            return httpx.Response(502, text="bad gateway")
        assert request.headers["accept"] == "application/json"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return httpx.Response(200, json={"error": "incorrect_client_credentials"})
        issued = self.codes.pop(form.get("code", ""), None)
        if issued is None:
            return httpx.Response(200, json={"error": "bad_verification_code"})
        if form.get("redirect_uri") != issued["redirect_uri"]:
            return httpx.Response(200, json={"error": "redirect_uri_mismatch"})
        if s256(form.get("code_verifier", "")) != issued["challenge"]:
            return httpx.Response(200, json={"error": "invalid_grant"})
        token = "gho_" + secrets.token_hex(16)
        self.tokens[token] = issued["account"]
        return httpx.Response(
            200, json={"access_token": token, "token_type": "bearer", "scope": "read:user"}
        )

    def _user(self, request: httpx.Request) -> httpx.Response:
        if "user_5xx" in self.fail:
            return httpx.Response(503, text="unavailable")
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        account = self.tokens.get(token)
        if account is None:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if self.user_body is not None:
            return httpx.Response(200, json=self.user_body)
        return httpx.Response(200, json=account.json())
