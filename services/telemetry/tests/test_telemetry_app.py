"""Wiring: settings, defaults of build_app, readiness, the retention job."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_authz import OpenFgaAuthorizer
from mowdeck_authz.testing import DenyAll
from mowdeck_contracts.v1 import CommandStatus
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client
from telemetry_support import API, T0, harness, make_settings, make_state

from telemetry.adapters.memory import MemoryStore
from telemetry.application.ports import RetentionTarget
from telemetry.domain.rules import history_row, position_row
from telemetry.main import build_app, default_principal_resolver, every, start_background_tasks


def test_telemetry_settings_defaults_and_config() -> None:
    settings = make_settings()
    config = settings.to_config()
    assert settings.service_name == "telemetry" and settings.http_port == 8000
    assert config.heartbeat_history_seconds == 300
    assert config.retention_history.days == 30
    assert config.retention_positions.days == 90
    assert config.retention_commands.days == 30
    assert config.query_max_range.days == 31 and config.query_max_rows == 5000
    assert settings.telemetry_retention_interval_seconds == 3600


def test_telemetry_settings_read_the_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOWDECK_TELEMETRY_RETENTION_HISTORY_DAYS", "7")
    monkeypatch.setenv("MOWDECK_TELEMETRY_HEARTBEAT_HISTORY_SECONDS", "120")
    from telemetry.settings import Settings

    settings = Settings.from_env()
    assert settings.telemetry_retention_history_days == 7
    assert settings.to_config().heartbeat_history_seconds == 120


def test_telemetry_build_app_needs_a_database() -> None:
    with pytest.raises(ValueError, match="MOWDECK_DATABASE_URL"):
        build_app(make_settings(), bus=InMemoryBus())


def test_telemetry_principal_resolver_from_the_environment() -> None:
    assert default_principal_resolver({}) is None
    url = {"MOWDECK_AUTH_JWKS_URL": "http://identity:8000/.well-known/jwks.json"}
    assert default_principal_resolver(url) is not None


async def test_telemetry_build_app_defaults_to_openfga_and_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOWDECK_OPENFGA_API_URL", "http://127.0.0.1:1")
    app = build_app(
        make_settings(database_url="postgresql://u:p@127.0.0.1:1/telemetry"), bus=InMemoryBus()
    )
    assert isinstance(app.state.authorizer, OpenFgaAuthorizer)
    in_memory = build_app(make_settings(), bus=InMemoryBus(), store=MemoryStore())
    async with lifespan_client(in_memory) as client:
        assert (await client.get("/healthz")).status_code == 200


async def test_telemetry_readiness_needs_the_subscriptions_and_the_bus() -> None:
    async with harness() as h:
        ready = await h.client.get("/readyz")
        assert ready.status_code == 200, ready.text
        assert {"nats", "subscriptions", "authz"} <= set(ready.json()["checks"])
        h.app.state.subscriptions = []
        no_subscriptions = await h.client.get("/readyz")
        assert no_subscriptions.status_code == 503
        assert no_subscriptions.json()["checks"]["subscriptions"]["status"] == "fail"
        h.app.state.subscriptions = [object()]
        await h.bus.close()
        assert (await h.client.get("/readyz")).status_code == 503


async def test_telemetry_without_a_bus_the_app_still_starts() -> None:
    app = build_app(
        make_settings(), store=MemoryStore(), authorizer=DenyAll(), background_tasks=True
    )
    async with lifespan_client(app) as client:
        assert (await client.get("/healthz")).status_code == 200


async def test_telemetry_retention_deletes_only_old_rows() -> None:
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        h.app.state.telemetry.retention._clock = lambda: now
        day = timedelta(days=1)
        data = h.store.data
        for at in (now - 31 * day, now - 29 * day):  # history keeps 30 days
            state = make_state(mower_id, owner.account_id, at=at)
            data.history[(mower_id, at)] = history_row(state)
        for at in (now - 91 * day, now - 89 * day):  # positions keep 90 days
            state = make_state(mower_id, owner.account_id, at=at, position=(1.0, 2.0))
            row = position_row(state)
            assert row is not None
            data.positions[(mower_id, at)] = row
        old = await h.publish_result(mower_id, CommandStatus.EXECUTED, at=now - 31 * day)
        new = await h.publish_result(mower_id, CommandStatus.EXECUTED, at=now - 29 * day)
        latest_before = dict(data.latest)
        deleted = await h.app.state.telemetry.retention.run_once()
        survivors = (
            set(data.history),
            set(data.positions),
            {c.command_id for c in data.commands.values()},
        )
    assert deleted == {
        RetentionTarget.HISTORY: 1,
        RetentionTarget.POSITIONS: 1,
        RetentionTarget.COMMANDS: 1,
    }
    assert survivors == (
        {(mower_id, now - 29 * day)},
        {(mower_id, now - 89 * day)},
        {new},
    )
    assert old != new and data.latest == latest_before and mower_id in data.directory


async def test_telemetry_retention_does_nothing_while_another_replica_holds_the_lock() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.publish_state(make_state(mower_id, owner.account_id, at=T0 - timedelta(days=400)))
        h.store.lock_held_elsewhere = True
        assert await h.app.state.telemetry.retention.run_once() is None
        assert len(h.store.data.history) == 1
        h.store.lock_held_elsewhere = False
        assert await h.app.state.telemetry.retention.run_once() is not None


async def test_telemetry_background_tasks_run_retention_and_survive_failures() -> None:
    async with harness(background_tasks=True, telemetry_retention_interval_seconds=10) as h:
        assert len(h.app.state.subscriptions) == 6
    calls = 0

    async def job() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")

    task = asyncio.create_task(every(0.01, "test", job))
    await asyncio.sleep(0.1)
    task.cancel()
    assert calls >= 2  # the first failure did not stop the loop


async def test_telemetry_start_background_tasks_without_a_bus_only_runs_retention() -> None:
    app = build_app(make_settings(), store=MemoryStore(), authorizer=DenyAll())
    async with lifespan_client(app):
        tasks = start_background_tasks(app, make_settings(), app.state.telemetry, None)
        assert len(tasks) == 3  # retention, tracking retention and the tracking tick
        for task in tasks:
            task.cancel()
    assert uuid4() and API
