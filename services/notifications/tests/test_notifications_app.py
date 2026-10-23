"""Startup and shutdown, settings, the background tasks and the VAPID key resolution."""

import asyncio
from pathlib import Path

import pytest
from mowdeck_authz.testing import AllowAll, InMemoryAuthorizer
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client
from notifications_support import API, ManualClock, harness, make_settings

from notifications.adapters.memory import MemoryStore
from notifications.main import (
    as_cache,
    build_app,
    default_principal_resolver,
    every,
    resolve_vapid,
)
from notifications.settings import Settings


def test_notifications_app_settings_defaults_and_config() -> None:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "notifications_battery_low_percent": 40,
            "notifications_battery_rearm_percent": 30,
            "notifications_channels": " webpush , log ,",
        }
    )
    config = settings.to_config()
    assert settings.service_name == "notifications" and settings.http_port == 8000
    assert settings.channel_names == ("webpush", "log")
    assert config.rules.battery_rearm_percent == 40  # never below the threshold
    assert config.ntfy_allowed_servers == ("https://ntfy.sh",)
    assert "push.services.mozilla.com" in config.webpush_allowed_hosts
    assert config.inbox_keep == 200 and config.max_attempts == 8


def test_notifications_app_empty_variables_mean_unset() -> None:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "notifications_vapid_private_key": "",
            "notifications_vapid_public_key": " ",
            "notifications_app_url": "",
            "notifications_vapid_subject": "mailto:ops@example.test",
        }
    )
    assert settings.notifications_vapid_private_key is None
    assert settings.notifications_vapid_public_key is None
    assert settings.notifications_app_url is None
    assert settings.notifications_vapid_subject == "mailto:ops@example.test"


def test_notifications_app_needs_a_database_url() -> None:
    with pytest.raises(ValueError, match="MOWDECK_DATABASE_URL"):
        build_app(make_settings(), authorizer=AllowAll())


def test_notifications_app_vapid_resolution(tmp_path: Path) -> None:
    assert resolve_vapid(make_settings()) is None
    path = tmp_path / "vapid_private_key"
    generated = resolve_vapid(make_settings(notifications_vapid_key_path=str(path)))
    assert generated is not None and path.exists()
    reused = resolve_vapid(make_settings(notifications_vapid_key_path=str(path)))
    assert reused is not None and reused.public_key == generated.public_key
    explicit = resolve_vapid(
        make_settings(
            notifications_vapid_private_key=path.read_text().strip(),
            notifications_vapid_public_key=generated.public_key,
        )
    )
    assert explicit is not None and explicit.public_key == generated.public_key


def test_notifications_app_default_resolver_and_cache() -> None:
    assert default_principal_resolver({}) is None
    assert default_principal_resolver({"MOWDECK_AUTH_JWKS_URL": "http://x.test/jwks"}) is not None
    assert as_cache(InMemoryAuthorizer()) is not None
    assert as_cache(AllowAll()) is None


async def test_notifications_app_startup_subscriptions_and_shutdown() -> None:
    bus = InMemoryBus()
    app = build_app(
        make_settings(),
        bus=bus,
        store=MemoryStore(),
        authorizer=InMemoryAuthorizer(),
        background_tasks=False,
    )
    async with lifespan_client(app) as client:
        assert (await client.get("/readyz")).status_code == 200
        assert len(app.state.subscriptions) == 9  # incl. entitlements.plan.changed and vision
    assert app.state.draining is True and not bus.is_connected


async def test_notifications_app_not_ready_while_the_outbox_is_stalled() -> None:
    clock = ManualClock()
    app = build_app(
        make_settings(notifications_stall_seconds=300),
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=InMemoryAuthorizer(),
        clock=clock,
        background_tasks=False,
    )
    async with lifespan_client(app) as client:
        assert (await client.get("/readyz")).status_code == 200
        clock.advance(301)  # no round ran: no worker progress for longer than the limit
        stalled = await client.get("/readyz")
        assert stalled.status_code == 503 and "outbox" in stalled.text
        await app.state.notifications.outbox.dispatch()  # an idle round is progress
        assert (await client.get("/readyz")).status_code == 200


async def test_notifications_app_without_bus_is_not_ready() -> None:
    app = build_app(
        make_settings(), store=MemoryStore(), authorizer=AllowAll(), background_tasks=False
    )
    async with lifespan_client(app) as client:
        assert (await client.get("/readyz")).status_code == 503


async def test_notifications_app_background_worker_delivers_and_sweeps() -> None:
    async with harness(
        background_tasks=True,
        notifications_worker_interval_seconds=0.05,
        notifications_offline_check_seconds=1,
    ) as h:
        owner = await h.owner()
        await h.add_mower(owner)
        response = await h.client.post(f"{API}/test", headers=owner.headers)
        assert response.status_code == 202
        for _ in range(100):
            if h.channel.sent:
                break
            await asyncio.sleep(0.02)
        assert len(h.channel.sent) == 1


async def test_notifications_app_every_logs_failures() -> None:
    calls: list[int] = []

    async def job() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first run fails")

    task = asyncio.create_task(every(0.01, "test", job))
    for _ in range(100):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 2
