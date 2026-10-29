"""Security center (ADR 0065): sessions and devices, revocation, security events, new-login
alerts, the password status and the last-way-in guard of linked providers."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from identity_support import API, PASSWORD, Harness, bearer, harness
from mowdeck_contracts.v1 import IDENTITY_SESSION_NEW_DEVICE, EventEnvelope, NewDeviceLogin

from identity.application.context import RequestContext
from identity.domain.devices import ClientKind, NetworkKind, describe_user_agent, ip_prefix
from identity.domain.models import ExternalIdentity

SESSIONS = f"{API}/me/sessions"
REAUTH = "urn:mowdeck:problem:reauth-required"
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"
IPHONE_APP = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MowDeckApp"
)
SAFARI_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Safari/605.1.15"
)
NEW_DEVICE = f"event.{IDENTITY_SESSION_NEW_DEVICE}"


async def login_from(
    h: Harness, username: str, user_agent: str, *, new_device: bool = True
) -> dict[str, Any]:
    """Log in as from another browser: without the cookies of earlier logins (unless
    ``new_device`` is False) and with ``user_agent``."""
    if new_device:
        h.client.cookies.clear()
    response = await h.client.post(
        f"{API}/session",
        json={"username": username, "password": PASSWORD},
        headers={"User-Agent": user_agent},
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def alerts(h: Harness) -> list[NewDeviceLogin]:
    return [m.model(EventEnvelope[NewDeviceLogin]).payload for m in h.bus.messages(NEW_DEVICE)]


# --- the device label and the address --------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "client", "browser", "os"),
    [
        (FIREFOX, ClientKind.BROWSER, "Firefox", "Windows"),
        (SAFARI_MAC, ClientKind.BROWSER, "Safari", "macOS"),
        (IPHONE_APP, ClientKind.APP, None, "iOS"),
        (
            "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/141.0.0.0 Mobile Safari/537.36",
            ClientKind.BROWSER,
            "Chrome",
            "Android",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
            ClientKind.BROWSER,
            "Edge",
            "Windows",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) CriOS/141.0 Mobile/15E148 Safari/604.1",
            ClientKind.BROWSER,
            "Chrome",
            "iOS",
        ),
        ("curl/8.10.1", ClientKind.SCRIPT, "curl", None),
        ("python-httpx/0.28.1", ClientKind.SCRIPT, "Python", None),
        ("", ClientKind.UNKNOWN, None, None),
    ],
)
def test_user_agents_become_a_device_without_versions(
    agent: str, client: ClientKind, browser: str | None, os: str | None
) -> None:
    info = describe_user_agent(agent)
    assert (info.client, info.browser, info.os) == (client, browser, os)


def test_device_labels() -> None:
    assert describe_user_agent(FIREFOX).label == "Firefox on Windows"
    assert describe_user_agent(IPHONE_APP).label == "MowDeck app on iOS"
    assert describe_user_agent("curl/8.1").label == "curl"
    assert describe_user_agent(None).label == "Unknown device"


@pytest.mark.parametrize(
    ("ip", "prefix", "network"),
    [
        ("8.8.4.4", "8.8.4.x", NetworkKind.PUBLIC),
        ("192.168.1.20", "192.168.1.x", NetworkKind.LOCAL),
        ("127.0.0.1", "127.0.0.x", NetworkKind.LOCAL),
        ("2a00:1450:4001:80b::200e", "2a00:1450:4001::/48", NetworkKind.PUBLIC),
        ("::ffff:9.9.9.9", "9.9.9.x", NetworkKind.PUBLIC),
        ("fd12:3456:789a::1", "fd12:3456:789a::/48", NetworkKind.LOCAL),
        ("fe80::1%eth0", "fe80::/48", NetworkKind.LOCAL),
        ("not an address", None, NetworkKind.UNKNOWN),
        (None, None, NetworkKind.UNKNOWN),
    ],
)
def test_addresses_are_shortened_to_their_network(
    ip: str | None, prefix: str | None, network: NetworkKind
) -> None:
    assert ip_prefix(ip) == (prefix, network)


# --- sessions ------------------------------------------------------------------------------


async def test_the_session_list_shows_every_device_and_marks_the_current_one() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        laptop = await login_from(h, "lukas", FIREFOX)
        phone = await login_from(h, "lukas", IPHONE_APP)
        listed = await h.client.get(SESSIONS, headers=bearer(phone["access_token"]))
        anna = await h.register("anna")
        foreign = (await h.client.get(SESSIONS, headers=anna.headers)).json()
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    rows = listed.json()
    assert len(rows) == 3  # register's login, the laptop and the phone
    assert rows[0]["current"] is True and rows[0]["client"] == "app" and rows[0]["os"] == "iOS"
    assert [r["current"] for r in rows[1:]] == [False, False]
    firefox = next(r for r in rows if r["browser"] == "Firefox")
    assert firefox["label"] == "Firefox on Windows"
    assert firefox["method"] == "pwd"
    assert firefox["account_id"] == str(lukas.account_id)
    assert firefox["network"] in ("local", "unknown")
    assert {r["id"] for r in rows}.isdisjoint({r["id"] for r in foreign})
    assert laptop["access_token"] != phone["access_token"]


async def test_revoking_another_session_ends_its_refresh_and_identity_access_at_once() -> None:
    async with harness() as h:
        await h.register("lukas")
        laptop = await login_from(h, "lukas", FIREFOX)
        phone = await login_from(h, "lukas", IPHONE_APP)
        rows = (await h.client.get(SESSIONS, headers=bearer(phone["access_token"]))).json()
        laptop_sid = next(r["id"] for r in rows if r["browser"] == "Firefox")
        revoked = await h.client.delete(
            f"{SESSIONS}/{laptop_sid}", headers=bearer(phone["access_token"])
        )
        again = await h.client.delete(
            f"{SESSIONS}/{laptop_sid}", headers=bearer(phone["access_token"])
        )
        refreshed = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": laptop["refresh_token"]}
        )
        laptop_me = await h.client.get(f"{API}/me", headers=bearer(laptop["access_token"]))
        phone_me = await h.client.get(f"{API}/me", headers=bearer(phone["access_token"]))
        after = (await h.client.get(SESSIONS, headers=bearer(phone["access_token"]))).json()
        events = (
            await h.client.get(f"{API}/me/security-events", headers=bearer(phone["access_token"]))
        ).json()
    assert revoked.status_code == 204
    assert again.status_code == 204  # idempotent
    assert refreshed.status_code == 401
    assert laptop_me.status_code == 401  # Identity refuses the ended session's token at once
    assert phone_me.status_code == 200
    assert laptop_sid not in {r["id"] for r in after}
    ended = [e for e in events if e["action"] == "session.revoked"]
    assert ended and ended[0]["details"] == {"session": laptop_sid, "current": False}


async def test_revoking_needs_a_recent_step_up_and_only_own_sessions() -> None:
    async with harness() as h:
        h.clock.advance(-timedelta(minutes=6))
        lukas = await h.register("lukas")
        h.clock.advance(timedelta(minutes=6))
        anna = await h.register("anna")
        anna_sid = (await h.client.get(SESSIONS, headers=anna.headers)).json()[0]["id"]
        stale = await h.client.delete(f"{SESSIONS}/{anna_sid}", headers=lukas.headers)
        stale_all = await h.client.post(f"{SESSIONS}/revoke-others", headers=lukas.headers)
        step = await h.client.post(
            f"{API}/me/step-up/password", headers=lukas.headers, json={"password": PASSWORD}
        )
        fresh = bearer(step.json()["access_token"])
        foreign = await h.client.delete(f"{SESSIONS}/{anna_sid}", headers=fresh)
        unknown = await h.client.delete(f"{SESSIONS}/{uuid4()}", headers=fresh)
        anna_still = await h.client.get(f"{API}/me", headers=anna.headers)
    assert stale.status_code == 401 and stale.json()["type"] == REAUTH
    assert stale_all.status_code == 401 and stale_all.json()["type"] == REAUTH
    assert foreign.status_code == 404
    assert unknown.status_code == 404
    assert anna_still.status_code == 200


async def test_sign_out_everywhere_else_keeps_only_the_current_session() -> None:
    async with harness() as h:
        await h.register("lukas")
        laptop = await login_from(h, "lukas", FIREFOX)
        tablet = await login_from(h, "lukas", SAFARI_MAC)
        phone = await login_from(h, "lukas", IPHONE_APP)
        ended = await h.client.post(
            f"{SESSIONS}/revoke-others", headers=bearer(phone["access_token"])
        )
        left = (await h.client.get(SESSIONS, headers=bearer(phone["access_token"]))).json()
        refreshes = [
            (
                await h.client.post(
                    f"{API}/session/refresh", json={"refresh_token": pair["refresh_token"]}
                )
            ).status_code
            for pair in (laptop, tablet, phone)
        ]
    assert ended.status_code == 200
    assert ended.json() == {"revoked": 3}  # register's login, laptop, tablet
    assert [r["current"] for r in left] == [True]
    assert refreshes == [401, 401, 200]


async def test_api_tokens_have_no_security_page() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        created = await h.client.post(
            f"{API}/me/api-tokens",
            headers=lukas.headers,
            json={"name": "script", "scopes": ["mower.view"], "expires_in_days": 5},
        )
        exchanged = await h.client.post(f"{API}/token", json={"token": created.json()["token"]})
        token = bearer(exchanged.json()["access_token"])
        answers = [
            (await h.client.get(SESSIONS, headers=token)).status_code,
            (await h.client.get(f"{API}/me/security", headers=token)).status_code,
            (await h.client.get(f"{API}/me/security-events", headers=token)).status_code,
            (await h.client.post(f"{SESSIONS}/revoke-others", headers=token)).status_code,
        ]
    assert answers == [403, 403, 403, 403]


# --- new-device alerts --------------------------------------------------------------------


async def test_a_login_from_a_new_device_publishes_the_alert_once() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")  # the first login: recorded silently
        await login_from(h, "lukas", FIREFOX, new_device=False)  # same browser (cookie)
        silent = list(alerts(h))
        stranger = await login_from(h, "lukas", FIREFOX)  # no cookie: a new device
        again = await login_from(h, "lukas", FIREFOX, new_device=False)
        found = alerts(h)
        stranger_current = [
            r
            for r in (await h.client.get(SESSIONS, headers=bearer(stranger["access_token"]))).json()
            if r["current"]
        ]
        async with h.store.transaction() as tx:
            devices = await tx.known_devices.find(user_id=lukas.user_id)
        audit = await h.audit(lukas, action="security.new_device")
    assert silent == []
    assert len(found) == 1
    alert = found[0]
    assert alert.user_id == lukas.user_id and alert.account_id == lukas.account_id
    assert alert.device == "Firefox on Windows" and alert.client == "browser"
    assert alert.method == "password"
    assert stranger_current[0]["id"] == str(alert.session_id)
    assert again["access_token"]
    assert len(devices) == 2
    assert all(len(d.token_hash) == 64 for d in devices)
    assert [e["details"]["alerted"] for e in audit] == [True]


async def test_the_device_cookie_is_httponly_lax_and_long_lived() -> None:
    async with harness() as h:
        await h.register("lukas")
        h.client.cookies.clear()
        response = await h.client.post(
            f"{API}/session", json={"username": "lukas", "password": PASSWORD}
        )
    cookie = next(c for c in response.headers.get_list("set-cookie") if "mowdeck_device=" in c)
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=lax" in lowered
    assert "path=/api/v1/identity" in lowered
    assert "max-age=34560000" in lowered


async def test_alerts_can_be_turned_off_with_a_step_up_and_on_without() -> None:
    async with harness() as h:
        h.clock.advance(-timedelta(minutes=6))
        lukas = await h.register("lukas")
        h.clock.advance(timedelta(minutes=6))
        url = f"{API}/me/security/alerts"
        stale = await h.client.put(url, headers=lukas.headers, json={"new_login_alerts": False})
        step = await h.client.post(
            f"{API}/me/step-up/password", headers=lukas.headers, json={"password": PASSWORD}
        )
        fresh = bearer(step.json()["access_token"])
        off = await h.client.put(url, headers=fresh, json={"new_login_alerts": False})
        overview_off = (await h.client.get(f"{API}/me/security", headers=fresh)).json()
        await login_from(h, "lukas", FIREFOX)
        muted = list(alerts(h))
        on = await h.client.put(url, headers=lukas.headers, json={"new_login_alerts": True})
        await login_from(h, "lukas", SAFARI_MAC)
        loud = alerts(h)
    assert stale.status_code == 401 and stale.json()["type"] == REAUTH
    assert off.status_code == 200 and off.json() == {"new_login_alerts": False}
    assert overview_off["new_login_alerts"] is False
    assert muted == []
    assert on.status_code == 200
    assert [a.device for a in loud] == ["Safari on macOS"]


async def test_the_app_handing_over_a_browser_login_raises_no_alert() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        async with h.store.transaction() as tx:
            found = await tx.users.get(id=lukas.user_id)
        assert found is not None
        services = h.app.state.identity
        ctx = RequestContext(ip="198.51.100.4", user_agent=IPHONE_APP)
        pair = await services.sessions._start_session(
            lukas.user_id, lukas.account_id, ctx, method="app_handoff"
        )
        async with h.store.transaction() as tx:
            devices = await tx.known_devices.count(user_id=lukas.user_id)
    assert pair.device_token is not None
    assert devices == 2
    assert alerts(h) == []


# --- overview and events -------------------------------------------------------------------


async def test_the_overview_shows_the_password_status_and_counts() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        first = (await h.client.get(f"{API}/me/security", headers=lukas.headers)).json()
        h.clock.advance(timedelta(minutes=1))
        changed = await h.client.put(
            f"{API}/me/password",
            headers=lukas.headers,
            json={"current_password": PASSWORD, "new_password": "another horse staple"},
        )
        second = (await h.client.get(f"{API}/me/security", headers=lukas.headers)).json()
    assert first["password_set"] is True
    assert first["password_changed_at"] is not None
    assert first["new_login_alerts"] is True
    assert first["sessions"] == 1 and first["known_devices"] == 1
    assert first["passkeys"] == 0 and first["providers"] == [] and first["personal_tokens"] == 0
    assert changed.status_code == 204
    assert second["password_changed_at"] > first["password_changed_at"]


async def test_security_events_are_the_callers_own_and_safe_to_show() -> None:
    async with harness() as h:
        # Tokens are checked against the real time: the old events happen in the past.
        h.clock.advance(-timedelta(days=40))
        lukas = await h.register("lukas")
        bad = await h.client.post(
            f"{API}/session", json={"username": "lukas", "password": "wrong password!"}
        )
        await login_from(h, "lukas", FIREFOX)
        h.clock.advance(timedelta(days=40))
        anna = await h.register("anna")
        annas = (await h.client.get(f"{API}/me/security-events", headers=anna.headers)).json()
        recent = await login_from(h, "lukas", SAFARI_MAC)
        events = await h.client.get(
            f"{API}/me/security-events", headers=bearer(recent["access_token"])
        )
        longer = await h.client.get(
            f"{API}/me/security-events",
            headers=bearer(recent["access_token"]),
            params={"days": 90},
        )
    assert bad.status_code == 401
    assert events.status_code == 200
    actions = [e["action"] for e in events.json()]
    assert actions == ["security.new_device", "session.login"]  # the last 30 days only
    old = [e["action"] for e in longer.json()]
    assert "session.login_failed" in old and old.count("security.new_device") == 2
    for event in longer.json():
        assert "jti" not in event["details"]
        assert set(event) == {
            "id",
            "at",
            "action",
            "outcome",
            "target_type",
            "details",
            "ip_prefix",
            "network",
        }
    assert all(e["action"] != "security.new_device" for e in annas)
    assert lukas.user_id != anna.user_id


async def test_unlinking_the_last_way_in_is_refused() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        async with h.store.transaction() as tx:
            await tx.external_identities.add(
                ExternalIdentity(
                    id=uuid4(),
                    user_id=lukas.user_id,
                    provider="github",
                    subject="4242",
                    linked_at=h.clock(),
                    display_name="lukas",
                )
            )
        with_password = await h.client.delete(
            f"{API}/me/external-identities/github", headers=lukas.headers
        )
        async with h.store.transaction() as tx:
            await tx.external_identities.add(
                ExternalIdentity(
                    id=uuid4(),
                    user_id=lukas.user_id,
                    provider="github",
                    subject="4243",
                    linked_at=h.clock(),
                )
            )
            found = await tx.users.get(id=lukas.user_id)
            assert found is not None
            found.password_hash = None
            await tx.users.update(found)
        last_way = await h.client.delete(
            f"{API}/me/external-identities/github", headers=lukas.headers
        )
    assert with_password.status_code == 204
    assert last_way.status_code == 409
