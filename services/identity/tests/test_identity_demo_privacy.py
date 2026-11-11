# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The public demo keeps no address and no browser string of a visitor; every other installation
keeps them (the security center's "your devices" view relies on them)."""

from datetime import UTC, datetime
from typing import Any

from identity_support import API, PASSWORD, bearer, harness
from mowdeck_service_kit.demo import DEMO_SEED_HEADER

from identity.application.context import IdentityConfig

SEED = "demo-seed-secret"
DEMO: dict[str, Any] = {"demo_mode": True, "demo_seed_token": SEED}
BROWSER = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/130.0"}
FROM = {"X-Forwarded-For": "203.0.113.7"}


NOW = datetime(2026, 11, 11, tzinfo=UTC)


def _config(**values: Any) -> IdentityConfig:
    return IdentityConfig(**values)


def test_the_demo_predicate_is_the_demo_user() -> None:
    assert _config(demo_username="demo").demo is True
    assert _config().demo is False


def test_the_context_of_the_demo_drops_address_and_browser_for_storage() -> None:
    ctx = _config(demo_username="demo").request_context(ip="203.0.113.7", user_agent="Firefox")
    assert ctx.ip == "203.0.113.7"  # still there for the in-memory rate limit
    assert ctx.user_agent == "Firefox"
    assert ctx.stored_ip is None
    assert ctx.stored_user_agent is None
    assert ctx.audit_entry(at=NOW, action="x", outcome="success").ip is None


def test_the_context_of_a_normal_installation_keeps_them() -> None:
    ctx = _config().request_context(ip="203.0.113.7", user_agent="Firefox")
    assert ctx.stored_ip == "203.0.113.7"
    assert ctx.stored_user_agent == "Firefox"
    assert ctx.audit_entry(at=NOW, action="x", outcome="success").ip == "203.0.113.7"
    assert _config().request_context(user_agent="x" * 300).stored_user_agent == "x" * 200
    assert _config().request_context(user_agent="").stored_user_agent is None


async def test_a_demo_visit_persists_no_address_and_no_browser() -> None:
    async with harness(**DEMO, identity_trust_forwarded_for=True) as h:
        seeded = await h.client.post(f"{API}/demo/seed", headers={DEMO_SEED_HEADER: SEED})
        assert seeded.status_code == 200, seeded.text
        opened = await h.client.post(
            f"{API}/demo/session", headers={**BROWSER, **FROM, "X-Mowdeck-Refresh": "cookie-only"}
        )
        assert opened.status_code == 200, opened.text
        token = opened.json()["access_token"]
        refreshed = await h.client.post(
            f"{API}/session/refresh",
            headers={**BROWSER, **FROM, "X-Requested-With": "mowdeck"},
        )
        assert refreshed.status_code == 200, refreshed.text
        sessions = await h.client.get(f"{API}/me/sessions", headers=bearer(token))
        assert sessions.status_code == 200, sessions.text
        assert sessions.json()[0]["ip_prefix"] is None
        rows = h.store.tables
        assert rows.sessions.rows, "the visit must have created a session"
        assert all(s.ip is None and s.user_agent is None for s in rows.sessions.rows.values())
        assert rows.login_attempts.rows
        assert all(a.ip is None for a in rows.login_attempts.rows.values())
        assert rows.audit.rows
        assert all(e.ip is None for e in rows.audit.rows.values())
        assert all(d.last_ip_prefix is None for d in rows.known_devices.rows.values())


async def test_the_demo_still_limits_per_address() -> None:
    async with harness(
        **DEMO,
        identity_trust_forwarded_for=True,
        identity_login_ip_burst=1,
        identity_login_ip_per_minute=1,
    ) as h:
        await h.client.post(f"{API}/demo/seed", headers={DEMO_SEED_HEADER: SEED})
        first = await h.client.post(f"{API}/demo/session", headers=FROM)
        second = await h.client.post(f"{API}/demo/session", headers=FROM)
        other = await h.client.post(
            f"{API}/demo/session", headers={"X-Forwarded-For": "198.51.100.9"}
        )
    assert first.status_code == 200
    assert second.status_code == 429
    assert other.status_code == 200  # the in-memory bucket is keyed per address


async def test_a_normal_installation_keeps_the_address_and_browser_of_a_session() -> None:
    async with harness(identity_trust_forwarded_for=True) as h:
        owner = await h.register("owner")
        response = await h.client.post(
            f"{API}/session",
            json={"username": "owner", "password": PASSWORD},
            headers={**BROWSER, **FROM},
        )
        assert response.status_code == 200, response.text
        rows = h.store.tables
        mine = [s for s in rows.sessions.rows.values() if s.user_agent == BROWSER["User-Agent"]]
        assert mine
        assert all(s.ip == "203.0.113.7" for s in mine)
        assert any(a.ip == "203.0.113.7" for a in rows.login_attempts.rows.values())
        assert any(e.ip == "203.0.113.7" for e in rows.audit.rows.values())
        sessions = await h.client.get(f"{API}/me/sessions", headers=owner.headers)
    assert "203.0.113.x" in [item["ip_prefix"] for item in sessions.json()]
