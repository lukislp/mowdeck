"""GitHub as an OAuth 2 login provider (ADR 0051), the ``OAuthClient`` port on httpx.

* Authorization: ``https://github.com/login/oauth/authorize`` with ``client_id``,
  ``redirect_uri``, ``scope=read:user``, ``state``, ``code_challenge`` (S256) and
  ``allow_signup=false``.
* Token: ``POST https://github.com/login/oauth/access_token`` (form, ``Accept:
  application/json``) with the client secret and the PKCE verifier. GitHub answers errors such as
  ``bad_verification_code`` with status 200 and an ``error`` field.
* Profile: ``GET https://api.github.com/user``; only ``id`` (the stable numeric account id, the
  subject) and ``login`` (shown to the user) are read. E-mail addresses are never requested
  (no ``user:email`` scope) nor used.
* The access token is used for that one request and then dropped; MowDeck never stores or
  logs it. It is deliberately not revoked: revoking an OAuth App's last token deletes the user's
  authorization, so GitHub would ask for consent and e-mail "a third-party OAuth application was
  authorized" at every sign-in (ADR 0051, amendment 2026-10-25). GitHub revokes OAuth tokens
  unused for a year; the user removes the authorization under github.com/settings/applications.

The endpoints are configurable for GitHub Enterprise Server and for the test stack's fake.
"""

from typing import Any
from urllib.parse import urlencode

import httpx

from identity.application.ports import ExternalProfile, ProviderRejected, ProviderUnavailable

SCOPE = "read:user"
API_VERSION = "2022-11-28"
MAX_LOGIN_LENGTH = 255


class GitHubOAuthClient:
    """The GitHub endpoints of the authorization code flow."""

    provider = "github"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        authorize_url: str = "https://github.com/login/oauth/authorize",
        token_url: str = "https://github.com/login/oauth/access_token",  # noqa: S107 - a URL
        api_url: str = "https://api.github.com",
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self._secret = client_secret
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport
        """Tests pass ``httpx.MockTransport``; None uses the network."""

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        """The GitHub authorization page for this round trip."""
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": SCOPE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "allow_signup": "false",
            }
        )
        return f"{self.authorize_url}?{query}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=False,
            headers={"User-Agent": "MowDeck-Identity"},
        )

    async def profile(self, *, code: str, code_verifier: str, redirect_uri: str) -> ExternalProfile:
        """Redeem the code and read the account's id and login."""
        async with self._client() as http:
            token = await self._token(http, code, code_verifier, redirect_uri)
            return await self._user(http, token)

    async def _token(
        self, http: httpx.AsyncClient, code: str, verifier: str, redirect_uri: str
    ) -> str:
        try:
            response = await http.post(
                self.token_url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self._secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"token endpoint: {type(exc).__name__}") from exc
        body = _json(response)
        if response.status_code >= 500:
            raise ProviderUnavailable(f"token endpoint answered {response.status_code}")
        error = body.get("error") if isinstance(body, dict) else None
        if error in ("incorrect_client_credentials", "redirect_uri_mismatch"):
            # The installation's settings are wrong, not the user's attempt.
            raise ProviderUnavailable(f"token endpoint: {error}")
        token = body.get("access_token") if isinstance(body, dict) else None
        if response.status_code != 200 or error or not isinstance(token, str) or not token:
            raise ProviderRejected(f"token endpoint: {error or response.status_code}")
        return token

    async def _user(self, http: httpx.AsyncClient, token: str) -> ExternalProfile:
        try:
            response = await http.get(
                f"{self.api_url}/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"user endpoint: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise ProviderUnavailable(f"user endpoint answered {response.status_code}")
        body = _json(response)
        if response.status_code != 200 or not isinstance(body, dict):
            raise ProviderRejected(f"user endpoint answered {response.status_code}")
        account_id = body.get("id")
        # bool is an int in Python; GitHub ids are positive integers.
        if not isinstance(account_id, int) or isinstance(account_id, bool) or account_id <= 0:
            raise ProviderRejected("user without a numeric id")
        login = body.get("login")
        name = login[:MAX_LOGIN_LENGTH] if isinstance(login, str) and login else None
        return ExternalProfile(subject=str(account_id), display_name=name)


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
