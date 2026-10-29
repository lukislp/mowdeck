"""Public demo (ADR 0068): the one-click sign-in, the seed call and what the demo refuses."""

from typing import Any
from uuid import uuid4

import pytest
from identity_support import API, PASSWORD, bearer, harness, make_settings
from mowdeck_service_kit.demo import DEMO_HEADER, DEMO_SEED_HEADER

SEED = "demo-seed-secret"
SEEDED = {DEMO_SEED_HEADER: SEED}
DEMO: dict[str, Any] = {"demo_mode": True, "demo_seed_token": SEED}


async def _seed(client: Any) -> dict[str, Any]:
    response = await client.post(f"{API}/demo/seed", headers=SEEDED)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def test_the_seed_creates_the_demo_account_once() -> None:
    async with harness(**DEMO) as h:
        first = await _seed(h.client)
        again = await _seed(h.client)
    assert first["created"] is True
    assert again == {**first, "created": False}


async def test_the_seed_needs_the_secret() -> None:
    async with harness(**DEMO) as h:
        without = await h.client.post(f"{API}/demo/seed")
        wrong = await h.client.post(f"{API}/demo/seed", headers={DEMO_SEED_HEADER: "guess"})
    assert without.status_code == 403
    assert wrong.status_code == 403


async def test_try_the_demo_signs_in_as_the_demo_user() -> None:
    async with harness(**DEMO) as h:
        seeded = await _seed(h.client)
        response = await h.client.post(
            f"{API}/demo/session", headers={"X-Mowdeck-Refresh": "cookie-only"}
        )
        assert response.status_code == 200, response.text
        assert response.headers[DEMO_HEADER] == "1"
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["refresh_token"] is None
        assert "mowdeck_refresh" in response.cookies
        me = await h.client.get(f"{API}/me", headers=bearer(body["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["username"] == "demo"
    assert me.json()["account_id"] == seeded["account_id"]


async def test_try_the_demo_before_the_seed_is_not_found() -> None:
    async with harness(**DEMO) as h:
        response = await h.client.post(f"{API}/demo/session")
    assert response.status_code == 404


async def test_no_demo_outside_demo_mode() -> None:
    async with harness(demo_seed_token=SEED) as h:
        session = await h.client.post(f"{API}/demo/session")
        seed = await h.client.post(f"{API}/demo/seed", headers=SEEDED)
        owner = await h.register("owner")
        me = await h.client.get(f"{API}/me", headers=owner.headers)
    assert session.status_code == 404
    assert seed.status_code == 404
    assert DEMO_HEADER not in me.headers


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/register", {"username": "x", "password": PASSWORD, "account_name": "X"}),
        ("POST", "/session", {"username": "demo", "password": PASSWORD}),
        ("POST", "/passkeys/challenge", None),
        ("POST", "/passkeys/session", {}),
        ("POST", "/oauth/github/start", {}),
        ("POST", "/app-login/start", {"code_challenge": "x" * 43}),
        ("POST", "/token", {"token": "mdk_x"}),
        ("POST", "/me/api-tokens", {"name": "script", "scopes": []}),
        ("POST", "/me/widget-credentials", {"device_name": "phone"}),
        ("PUT", "/me/password", {"current_password": PASSWORD, "new_password": PASSWORD}),
        ("POST", "/me/step-up/password", {"password": PASSWORD}),
        ("POST", "/me/passkeys/options", None),
        ("POST", "/session/logout-all", None),
        # Security center (ADR 0065): alerts and other visitors' sessions stay as they are.
        ("PUT", "/me/security/alerts", {"new_login_alerts": False}),
        ("DELETE", f"/me/sessions/{uuid4()}", None),
        ("POST", "/me/sessions/revoke-others", None),
    ],
)
async def test_the_demo_refuses_sign_in_methods_tokens_and_security(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    async with harness(**DEMO) as h:
        await _seed(h.client)
        token = (await h.client.post(f"{API}/demo/session")).json()["access_token"]
        response = await h.client.request(method, f"{API}{path}", json=body, headers=bearer(token))
    assert response.status_code == 403, response.text
    assert response.json()["type"] == "urn:mowdeck:problem:demo-mode"


async def test_the_demo_refuses_account_users_and_roles() -> None:
    async with harness(**DEMO) as h:
        seeded = await _seed(h.client)
        headers = bearer((await h.client.post(f"{API}/demo/session")).json()["access_token"])
        base = f"{API}/accounts/{seeded['account_id']}"
        calls = [
            await h.client.patch(base, json={"name": "Mine now"}, headers=headers),
            await h.client.delete(base, headers=headers),
            await h.client.post(
                f"{base}/users",
                json={"username": "friend", "password": PASSWORD},
                headers=headers,
            ),
            await h.client.post(f"{base}/roles", json={"name": "x"}, headers=headers),
            await h.client.post(f"{base}/api-tokens", json={"name": "x"}, headers=headers),
        ]
        read = await h.client.get(base, headers=headers)
    assert [c.status_code for c in calls] == [403] * len(calls)
    assert read.status_code == 200, read.text


async def test_the_demo_shows_the_security_center_read_only() -> None:
    async with harness(**DEMO) as h:
        await _seed(h.client)
        headers = bearer((await h.client.post(f"{API}/demo/session")).json()["access_token"])
        reads = [
            await h.client.get(f"{API}/me/security", headers=headers),
            await h.client.get(f"{API}/me/sessions", headers=headers),
            await h.client.get(f"{API}/me/security-events", headers=headers),
        ]
    assert [r.status_code for r in reads] == [200, 200, 200], [r.text for r in reads]


async def test_demo_sign_ins_remember_no_device_and_alert_nobody() -> None:
    async with harness(**DEMO) as h:
        await _seed(h.client)
        first = await h.client.post(f"{API}/demo/session")
        second = await h.client.post(f"{API}/demo/session")
        headers = bearer(second.json()["access_token"])
        overview = await h.client.get(f"{API}/me/security", headers=headers)
        events = await h.client.get(f"{API}/me/security-events", headers=headers)
    assert "mowdeck_device" not in first.cookies
    assert overview.json()["known_devices"] == 0
    assert "security.new_device" not in events.text


async def test_the_demo_keeps_the_session_and_the_tour() -> None:
    async with harness(**DEMO) as h:
        await _seed(h.client)
        signed_in = await h.client.post(f"{API}/demo/session")
        refresh_token = signed_in.json()["refresh_token"]
        headers = bearer(signed_in.json()["access_token"])
        tour = await h.client.put(
            f"{API}/me/onboarding", json={"status": "completed"}, headers=headers
        )
        refreshed = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": refresh_token}
        )
        logout = await h.client.post(
            f"{API}/session/logout", json={"refresh_token": refreshed.json()["refresh_token"]}
        )
    assert tour.status_code == 200, tour.text
    assert refreshed.status_code == 200, refreshed.text
    assert logout.status_code == 204, logout.text


def test_demo_mode_closes_registration_and_the_other_sign_in_methods() -> None:
    settings = make_settings(
        demo_mode=True,
        identity_registration="open",
        identity_public_url="https://demo.example.org",
        identity_github_client_id="client",
        identity_github_client_secret="secret",  # noqa: S106 - test value
    )
    config = settings.to_config()
    assert config.registration.value == "closed"
    assert config.passkey_rp is None
    assert config.demo_username == "demo"
    assert settings.github_login() is None
    assert settings.apple_login() is None
    assert make_settings().to_config().demo_username is None
