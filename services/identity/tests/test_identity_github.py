"""Sign in with GitHub (ADR 0051): linking, the login, unlinking, the round trip's state and PKCE,
refusals, rate limits, the app hand-off and the settings.

GitHub is the fake of ``github_support`` behind the real ``GitHubOAuthClient`` (httpx with a mock
transport), so the requests MowDeck sends to GitHub are checked as GitHub would.
"""

import logging
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from github_support import (
    CALLBACK,
    CLIENT_ID,
    CLIENT_SECRET,
    SETTINGS,
    FakeGitHub,
    GitHubAccount,
)
from identity_support import API, PASSWORD, Account, Harness, bearer, harness, make_settings

from identity.adapters.github import GitHubOAuthClient
from identity.application.ports import Lt, ProviderRejected, ProviderUnavailable
from identity.domain.credentials import clean_return_query, s256_challenge
from identity.domain.models import ExternalIdentity, oauth_callback_url

OCTO = GitHubAccount(id=583231, login="octocat", email="lukas@example.org")
OTHER = GitHubAccount(id=9919, login="hubot")
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"


def where(response: httpx.Response) -> tuple[str, dict[str, str]]:
    """Path (with fragment) and query of a redirect."""
    assert response.status_code == 303, response.text
    parts = urlsplit(response.headers["location"])
    assert not parts.scheme and not parts.netloc  # always a local page
    path = parts.path + (f"#{parts.fragment}" if parts.fragment else "")
    return path, {k: v[0] for k, v in parse_qs(parts.query).items()}


async def start_link(h: Harness, who: Account, password: str = PASSWORD) -> httpx.Response:
    return await h.client.post(
        f"{API}/me/external-identities/github/link",
        headers=who.headers,
        json={"password": password},
    )


async def callback(h: Harness, query: dict[str, str]) -> httpx.Response:
    return await h.client.get(f"{API}/oauth/github/callback", params=query)


async def link(h: Harness, gh: FakeGitHub, who: Account, account: GitHubAccount) -> httpx.Response:
    """Link ``account`` to ``who``; returns the callback's response."""
    started = await start_link(h, who)
    assert started.status_code == 200, started.text
    return await callback(h, gh.authorize(started.json()["authorize_url"], account))


async def start_login(h: Harness, return_query: str | None = None) -> httpx.Response:
    body = {"return_query": return_query} if return_query is not None else None
    started = await h.client.post(f"{API}/oauth/github/start", json=body)
    assert started.status_code == 200, started.text
    return started


async def github_login(
    h: Harness, gh: FakeGitHub, account: GitHubAccount, return_query: str | None = None
) -> httpx.Response:
    """A whole sign-in with GitHub; returns the callback's response."""
    started = await start_login(h, return_query)
    return await callback(h, gh.authorize(started.json()["authorize_url"], account))


async def refresh_with_cookie(h: Harness) -> httpx.Response:
    return await h.client.post(
        f"{API}/session/refresh", headers={"X-Requested-With": "test"}, json=None
    )


async def setup(h: Harness) -> Account:
    return await h.register("lukas")


# --- settings ------------------------------------------------------------------------------------


def test_callback_url_from_the_public_url() -> None:
    assert oauth_callback_url("https://mowdeck.example.org/", "github") == CALLBACK
    assert (
        oauth_callback_url("https://Mow.Example.org:8443", "github")
        == "https://mow.example.org:8443/api/v1/identity/oauth/github/callback"
    )
    assert oauth_callback_url("http://localhost:8480", "github").startswith("http://localhost")
    assert oauth_callback_url("http://127.0.0.1", "github").startswith("http://127.0.0.1/")
    for bad in ("", "ftp://x.org", "http://mowdeck.lan", "https://user:pw@x.org", "x.org"):
        with pytest.raises(ValueError, match=r"."):
            oauth_callback_url(bad, "github")


def test_settings_turn_github_on_only_when_complete(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    # Depending on the logging setup of earlier tests the lines go to stdout or to logging.
    caplog.set_level(logging.INFO)
    assert make_settings().github_login() is None
    on = make_settings(**SETTINGS).github_login()
    assert on is not None and on.redirect_uri == CALLBACK and on.client_id == CLIENT_ID
    assert CLIENT_SECRET not in repr(on)
    incomplete = [
        {**SETTINGS, "identity_github_client_secret": None},
        {**SETTINGS, "identity_github_client_id": ""},
        {**SETTINGS, "identity_public_url": ""},
        {**SETTINGS, "identity_public_url": "http://mowdeck.lan"},
    ]
    for values in incomplete:
        assert make_settings(**values).github_login() is None
    captured = capsys.readouterr()
    text = captured.out + captured.err + caplog.text
    assert "GitHub login is on" in text and CALLBACK in text
    assert text.count("GitHub login is off") == len(incomplete)
    assert CLIENT_SECRET not in text


def test_the_return_query_keeps_only_what_the_sign_in_page_needs() -> None:
    request = str(uuid4())
    assert clean_return_query(None) is None
    assert clean_return_query("?next=/mowers") == "next=%2Fmowers"
    kept = clean_return_query(f"app_request={request}&app_state=abc_123-X&evil=1&next=/x?y=1")
    assert kept is not None
    assert parse_qs(kept) == {
        "app_request": [request],
        "app_state": ["abc_123-X"],
        "next": ["/x?y=1"],
    }
    for hostile in (
        "next=//evil.example",
        "next=/\\evil.example",
        "next=https://evil.example",
        "next=/a\\b",
        "app_request=not-a-uuid",
        "app_state=<script>",
        "redirect=https://evil.example",
    ):
        assert clean_return_query(hostile) is None, hostile
    assert clean_return_query("next=/a&next=/b") == "next=%2Fa"


async def test_github_is_off_unless_configured() -> None:
    async with harness() as h:
        lukas = await setup(h)
        methods = (await h.client.get(f"{API}/login-methods")).json()
        start = await h.client.post(f"{API}/oauth/github/start")
        cb = await callback(h, {"state": "x", "code": "y"})
        linking = await start_link(h, lukas)
        links = await h.client.get(f"{API}/me/external-identities", headers=lukas.headers)
    assert methods == {"password": True, "passkey": False, "github": False}
    assert start.status_code == 404 and cb.status_code == 404 and linking.status_code == 404
    assert links.json() == []


# --- link, login, unlink ------------------------------------------------------------------------


async def test_link_then_sign_in_with_github_then_unlink() -> None:
    gh = FakeGitHub()
    async with harness(github_transport=gh.transport, **SETTINGS) as h:
        lukas = await setup(h)
        methods = (await h.client.get(f"{API}/login-methods")).json()
        linked = await link(h, gh, lukas, OCTO)
        links = (await h.client.get(f"{API}/me/external-identities", headers=lukas.headers)).json()
        h.client.cookies.clear()
        login = await github_login(h, gh, OCTO, "next=/schedules")
        refreshed = await refresh_with_cookie(h)
        me = await h.client.get(f"{API}/me", headers=bearer(refreshed.json()["access_token"]))
        after = (await h.client.get(f"{API}/me/external-identities", headers=lukas.headers)).json()
        unlinked = await h.client.delete(
            f"{API}/me/external-identities/github", headers=lukas.headers
        )
        again = await h.client.delete(f"{API}/me/external-identities/github", headers=lukas.headers)
        h.client.cookies.clear()
        refused = await github_login(h, gh, OCTO)
        password = await h.client.post(
            f"{API}/session", json={"username": "lukas", "password": PASSWORD}
        )
        audit = await h.audit(lukas)
    assert methods["github"] is True
    assert where(linked) == ("/settings#profile", {"linked": "github"})
    assert [(x["provider"], x["display_name"]) for x in links] == [("github", "octocat")]
    assert links[0]["last_used_at"] is None
    assert where(login) == ("/login", {"next": "/schedules", "signed_in_with": "github"})
    cookie = login.headers["set-cookie"]
    assert "mowdeck_refresh=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert login.headers["cache-control"] == "no-store"
    assert login.headers["referrer-policy"] == "no-referrer"
    assert refreshed.status_code == 200, refreshed.text
    assert me.json()["subject"] == str(lukas.user_id)
    assert after[0]["last_used_at"] is not None
    assert unlinked.status_code == 204 and again.status_code == 404
    assert where(refused)[1]["sign_in_error"] == "denied"
    assert password.status_code == 200  # the password always stays
    actions = [e["action"] for e in audit]
    assert {"external_identity.linked", "external_identity.unlinked"} <= set(actions)
    login_entry = next(
        e for e in audit if e["action"] == "session.login" and e["details"]["method"] == "github"
    )
    assert login_entry["details"]["provider_subject"] == str(OCTO.id)
    assert len(gh.revoked) == 3  # every GitHub token is revoked after its one request
    assert not gh.tokens
    # Only the account's id and login were asked for: no e-mail endpoint, no other scope.
    assert not any("emails" in r for r in gh.requests)


async def test_an_unknown_github_account_is_refused_and_never_registered() -> None:
    gh = FakeGitHub()
    async with harness(github_transport=gh.transport, **SETTINGS) as h:
        lukas = await setup(h)
        login = await github_login(h, gh, OTHER, "next=/mowers")
        async with h.store.transaction() as tx:
            users = await tx.users.count()
            attempts = await tx.login_attempts.count(success=False)
        audit = await h.audit(lukas)
    path, query = where(login)
    assert (path, query["sign_in_error"], query["provider"]) == ("/login", "denied", "github")
    assert query["next"] == "/mowers"
    assert "mowdeck_refresh" not in login.headers.get("set-cookie", "")
    assert users == 1
    assert attempts == 0  # nothing to guess; the password login is never throttled by it
    assert not [e for e in audit if e["action"] == "session.login_failed"]  # no user, no account


async def test_accounts_are_never_matched_by_e_mail_address() -> None:
    gh = FakeGitHub()
    async with harness(github_transport=gh.transport, **SETTINGS) as h:
        response = await h.client.post(
            f"{API}/register",
            json={
                "username": "lukas",
                "password": PASSWORD,
                "account_name": "home",
                "email": OCTO.email,
            },
        )
        # The GitHub account has the same (verified) address as the MowDeck user.
        login = await github_login(h, gh, OCTO)
        async with h.store.transaction() as tx:
            links = await tx.external_identities.count()
    assert response.status_code == 201
    assert where(login)[1]["sign_in_error"] == "denied"
    assert links == 0


async def test_linking_needs_the_password_and_the_login_limits_apply() -> None:
    gh = FakeGitHub()
    async with harness(
        github_transport=gh.transport, identity_login_max_failures=2, **SETTINGS
    ) as h:
        lukas = await setup(h)
        wrong = [await start_link(h, lukas, "wrong password!") for _ in range(2)]
        throttled = await start_link(h, lukas)
        async with h.store.transaction() as tx:
            attempts = await tx.login_attempts.count(success=False, username_norm="lukas")
        audit = await h.audit(lukas, action="external_identity.reauth_failed")
        no_token = await h.client.post(
            f"{API}/me/external-identities/github/link", json={"password": PASSWORD}
        )
    assert [w.status_code for w in wrong] == [401, 401]
    assert throttled.status_code == 429
    assert attempts == 2 and len(audit) == 2
    assert no_token.status_code == 401


async def test_a_github_account_belongs_to_one_user_and_a_user_has_one() -> None:
    gh = FakeGitHub()
    async with harness(github_transport=gh.transport, **SETTINGS) as h:
        lukas = await setup(h)
        bob = await h.register("bob")
        first = await link(h, gh, lukas, OCTO)
        second_for_lukas = await start_link(h, lukas)
        taken = await link(h, gh, bob, OCTO)
        bobs = await link(h, gh, bob, OTHER)
        bob_audit = await h.audit(bob, action="external_identity.link_failed")
        async with h.store.transaction() as tx:
            rows = await tx.external_identities.find(order_by="subject")
    assert where(first)[1] == {"linked": "github"}
    assert second_for_lukas.status_code == 409
    assert where(taken) == ("/settings#profile", {"link_error": "taken", "provider": "github"})
    assert where(bobs)[1] == {"linked": "github"}
    assert [(r.subject, r.user_id) for r in rows] == [
        (str(OCTO.id), lukas.user_id),
        (str(OTHER.id), bob.user_id),
    ]
    assert bob_audit[0]["details"]["reason"] == "provider account linked to another user"


async def test_linking_the_same_account_again_and_a_user_changing_meanwhile() -> None:
    gh = FakeGitHub()
    async with harness(github_transport=gh.transport, **SETTINGS) as h:
        lukas = await setup(h)
        started = await start_link(h, lukas)
        url = started.json()["authorize_url"]
        # Another tab linked a different GitHub account meanwhile.
        async with h.store.transaction() as tx:
            await tx.external_identities.add(
                ExternalIdentity(uuid4(), lukas.user_id, "github", str(OTHER.id), h.clock())
            )
        already = await callback(h, gh.authorize(url, OCTO))
        started = await start_link(h, lukas)
    assert where(already)[1]["link_error"] == "already"
    assert started.status_code == 409


async def test_disabled_users_cannot_sign_in_with_github() -> None:
    gh = FakeGitHub()
    async with harness(github_transport=gh.transport, **SETTINGS) as h:
        lukas = await setup(h)
        anna_id, anna_token = await h.subuser(lukas, "anna")
        anna = Account(lukas.account_id, anna_id, "anna", anna_token, "")
        linked = await link(h, gh, anna, OCTO)
        await h.client.post(lukas.url(f"/users/{anna_id}/disable"), headers=lukas.headers)
        login = await github_login(h, gh, OCTO)
        audit = await h.audit(lukas, action="session.login_failed")
        removed = await h.client.delete(lukas.url(f"/users/{anna_id}"), headers=lukas.headers)
        async with h.store.transaction() as tx:
            left = await tx.external_identities.count()
    assert where(linked)[1] == {"linked": "github"}
    assert where(login)[1]["sign_in_error"] == "denied"
    assert audit[0]["details"]["method"] == "github"
    assert audit[0]["details"]["reason"] == "user disabled"
    assert removed.status_code == 204 and left == 0


# --- the round trip: state, browser binding, PKCE, provider errors -------------------------------


async def test_the_round_trip_is_single_use_bound_to_the_browser_and_short_lived() -> None:
    gh = FakeGitHub()
    async with harness(github_transport=gh.transport, **SETTINGS) as h:
        lukas = await setup(h)
        await link(h, gh, lukas, OCTO)
        started = await start_login(h)
        set_cookie = started.headers["set-cookie"]
        query = gh.authorize(started.json()["authorize_url"], OCTO)
        first = await callback(h, query)
        replay = await callback(h, query)

        started = await start_login(h)
        query = gh.authorize(started.json()["authorize_url"], OCTO)
        h.client.cookies.clear()  # another browser got the link
        other_browser = await callback(h, query)

        started = await start_login(h)
        query = gh.authorize(started.json()["authorize_url"], OCTO)
        h.client.cookies.clear()
        wrong_secret = await h.client.get(
            f"{API}/oauth/github/callback",
            params=query,
            headers={"Cookie": f"mowdeck_oauth={VERIFIER}"},
        )

        started = await start_login(h)
        query = gh.authorize(started.json()["authorize_url"], OCTO)
        h.clock.advance(timedelta(minutes=11))
        expired = await callback(h, query)

        unknown = await callback(h, {"state": "x" * 43, "code": "abc"})
        no_state = await callback(h, {"code": "abc"})
    assert "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie
    assert "Path=/api/v1/identity/oauth/" in set_cookie and "Max-Age=600" in set_cookie
    assert where(first)[1]["signed_in_with"] == "github"
    assert "mowdeck_oauth=" in first.headers["set-cookie"]  # the binding cookie is dropped
    for response in (replay, other_browser, wrong_secret, expired, unknown, no_state):
        assert where(response)[1]["sign_in_error"] == "expired"
        assert "mowdeck_refresh=" not in response.headers.get("set-cookie", "")


async def test_pkce_the_provider_checks_the_verifier_of_this_round_trip() -> None:
    gh = FakeGitHub()
    async with harness(github_transport=gh.transport, **SETTINGS) as h:
        lukas = await setup(h)
        await link(h, gh, lukas, OCTO)
        started = await start_login(h)
        url = started.json()["authorize_url"]
        query = gh.authorize(url, OCTO)
        # GitHub got the stored challenge; the verifier stays in the browser's cookie (the fake
        # checks S256(verifier) against it at the token endpoint in every successful login).
        challenge = parse_qs(urlsplit(url).query)["code_challenge"][0]
        async with h.store.transaction() as tx:
            [stored] = await tx.oauth_states.find()
        assert stored.code_challenge == challenge
        assert h.client.cookies.get("mowdeck_oauth", path="/api/v1/identity/oauth/")
        # A code issued for another challenge does not pass GitHub's PKCE check.
        gh.codes[query["code"]]["challenge"] = s256_challenge(VERIFIER)
        refused = await callback(h, query)
    assert where(refused)[1]["sign_in_error"] == "denied"


async def test_provider_errors_end_the_round_trip() -> None:
    gh = FakeGitHub()
    async with harness(github_transport=gh.transport, **SETTINGS) as h:
        lukas = await setup(h)
        await link(h, gh, lukas, OCTO)
        results: dict[str, Any] = {}
        started = await start_login(h)
        results["cancelled"] = await callback(h, gh.deny(started.json()["authorize_url"]))
        for fail in ("token_5xx", "user_5xx", "network"):
            started = await start_login(h)
            query = gh.authorize(started.json()["authorize_url"], OCTO)
            gh.fail = {fail}
            results[fail] = await callback(h, query)
            gh.fail = set()
        started = await start_login(h)
        query = gh.authorize(started.json()["authorize_url"], OCTO)
        results["bad_code"] = await callback(h, {**query, "code": "not-issued"})
        started = await start_login(h)
        state = parse_qs(urlsplit(started.json()["authorize_url"]).query)["state"][0]
        results["misconfigured"] = await callback(
            h, {"state": state, "error": "redirect_uri_mismatch"}
        )
        started = await start_login(h)
        query = gh.authorize(started.json()["authorize_url"], OCTO)
        gh.user_body = {"login": "no-id"}
        results["no_id"] = await callback(h, query)
        gh.user_body = None
        bob = await h.register("bob")
        start_after_link = await start_link(h, bob)
        link_cancelled = await callback(h, gh.deny(start_after_link.json()["authorize_url"]))
    expected = {
        "cancelled": "cancelled",
        "token_5xx": "unavailable",
        "user_5xx": "unavailable",
        "network": "unavailable",
        "bad_code": "denied",
        "misconfigured": "unavailable",
        "no_id": "denied",
    }
    assert {k: where(v)[1]["sign_in_error"] for k, v in results.items()} == expected
    assert where(link_cancelled)[1]["link_error"] == "cancelled"


async def test_the_github_client_reads_only_a_numeric_id_and_the_login() -> None:
    def client(handler: Any) -> GitHubOAuthClient:
        return GitHubOAuthClient(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            transport=httpx.MockTransport(handler),
        )

    def github(user: Any, token: dict[str, Any] | None = None, revoke: int = 204) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login/oauth/access_token":
                return httpx.Response(200, json=token or {"access_token": "gho_x"})
            if request.method == "DELETE":
                return httpx.Response(revoke)
            return httpx.Response(200, json=user)

        return handler

    args = {"code": "c", "code_verifier": VERIFIER, "redirect_uri": CALLBACK}
    profile = await client(github({"id": 7, "login": "x" * 300}, revoke=500)).profile(**args)
    assert profile.subject == "7" and profile.display_name == "x" * 255
    profile = await client(github({"id": 8})).profile(**args)
    assert profile.display_name is None
    for user in ({"id": True}, {"id": -1}, {"id": "7"}, {}, [1]):
        with pytest.raises(ProviderRejected):
            await client(github(user)).profile(**args)
    with pytest.raises(ProviderUnavailable):
        await client(github({}, {"error": "incorrect_client_credentials"})).profile(**args)
    with pytest.raises(ProviderRejected):
        await client(github({}, {"token_type": "bearer"})).profile(**args)
    url = client(github({})).authorization_url(state="s", code_challenge="c", redirect_uri=CALLBACK)
    assert url.startswith("https://github.com/login/oauth/authorize?client_id=Iv1.mowdecktest&")


# --- limits, the app hand-off, housekeeping ------------------------------------------------------


async def test_callbacks_have_the_login_limit_and_starts_the_looser_one() -> None:
    gh = FakeGitHub()
    async with harness(
        github_transport=gh.transport,
        identity_login_ip_burst=2,
        identity_login_ip_per_minute=1,
        **SETTINGS,
    ) as h:
        starts = [await h.client.post(f"{API}/oauth/github/start") for _ in range(31)]
        callbacks = [await callback(h, {"state": "x" * 43, "code": "c"}) for _ in range(3)]
    assert [s.status_code for s in starts[:30]] == [200] * 30
    assert starts[30].status_code == 429
    assert [where(c)[1]["sign_in_error"] for c in callbacks] == [
        "expired",
        "expired",
        "rate_limited",
    ]


async def test_a_github_login_can_be_handed_to_the_app() -> None:
    gh = FakeGitHub()
    async with harness(github_transport=gh.transport, **SETTINGS) as h:
        lukas = await setup(h)
        await link(h, gh, lukas, OCTO)
        started = await h.client.post(
            f"{API}/app-login/start", json={"code_challenge": s256_challenge(VERIFIER)}
        )
        request_id = started.json()["request_id"]
        h.client.cookies.clear()
        login = await github_login(h, gh, OCTO, f"app_request={request_id}&app_state=st4te")
        refreshed = await refresh_with_cookie(h)
        code = await h.client.post(
            f"{API}/app-login/code",
            headers=bearer(refreshed.json()["access_token"]),
            json={"request_id": request_id},
        )
        app = await h.client.post(
            f"{API}/app-login/session",
            json={"code": code.json()["code"], "code_verifier": VERIFIER},
        )
    assert where(login) == (
        "/login",
        {"app_request": request_id, "app_state": "st4te", "signed_in_with": "github"},
    )
    assert code.status_code == 200, code.text
    assert app.status_code == 200 and app.json()["account_id"] == str(lukas.account_id)


async def test_round_trips_expire_and_are_purged() -> None:
    gh = FakeGitHub()
    async with harness(github_transport=gh.transport, **SETTINGS) as h:
        lukas = await setup(h)
        await start_login(h)
        await start_link(h, lukas)
        h.clock.advance(timedelta(minutes=11))
        async with h.store.transaction() as tx:
            assert await tx.oauth_states.delete(expires_at=Lt(h.clock())) == 2


async def test_only_users_link_and_unknown_providers_are_404() -> None:
    gh = FakeGitHub()
    async with harness(github_transport=gh.transport, **SETTINGS) as h:
        lukas = await setup(h)
        made = await h.client.post(
            lukas.url("/api-tokens"),
            headers=lukas.headers,
            json={
                "name": "ha",
                "grants": [
                    {
                        "level": {"type": "account", "id": str(lukas.account_id)},
                        "permissions": ["mower.view"],
                    }
                ],
            },
        )
        exchanged = await h.client.post(f"{API}/token", json={"token": made.json()["token"]})
        api = bearer(exchanged.json()["access_token"])
        token_link = await h.client.post(
            f"{API}/me/external-identities/github/link", headers=api, json={"password": PASSWORD}
        )
        token_list = await h.client.get(f"{API}/me/external-identities", headers=api)
        unknown = await h.client.post(f"{API}/oauth/gitlab/start")
        bad_name = await h.client.delete(
            f"{API}/me/external-identities/GitHub!", headers=lukas.headers
        )
    assert token_link.status_code == 403 and token_list.status_code == 403
    assert unknown.status_code == 404 and bad_name.status_code == 404


async def test_a_link_survives_turning_github_off_and_can_still_be_removed() -> None:
    async with harness() as h:
        lukas = await setup(h)
        async with h.store.transaction() as tx:
            await tx.external_identities.add(
                ExternalIdentity(
                    uuid4(), lukas.user_id, "github", str(OCTO.id), h.clock(), "octocat"
                )
            )
        listed = await h.client.get(f"{API}/me/external-identities", headers=lukas.headers)
        removed = await h.client.delete(
            f"{API}/me/external-identities/github", headers=lukas.headers
        )
    assert [x["display_name"] for x in listed.json()] == ["octocat"]
    assert removed.status_code == 204
    assert UUID(str(lukas.user_id))
