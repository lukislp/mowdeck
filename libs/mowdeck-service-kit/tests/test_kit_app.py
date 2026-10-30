"""create_app lifecycle, graceful shutdown, bus wiring, tracing, db helpers and run()."""

import asyncio
import os
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from mowdeck_service_kit import Bus, create_app, run
from mowdeck_service_kit.app import API_PREFIX
from mowdeck_service_kit.bus import InMemoryBus, JetStreamBus
from mowdeck_service_kit.db import (
    async_database_url,
    create_engine,
    readiness_check_db,
    session_dependency,
)
from mowdeck_service_kit.health import run_check
from mowdeck_service_kit.otel import traces_endpoint
from mowdeck_service_kit.testing import (
    lifespan_client,
    make_test_app,
    make_test_descriptor,
    make_test_settings,
)
from pydantic import SecretStr

router = APIRouter()


@router.post("/announce")
async def announce(bus: Bus) -> dict[str, bool]:
    result = await bus.publish("event.test.announced", b"{}")
    return {"duplicate": result.duplicate}


async def test_startup_and_shutdown_hooks_run_in_order() -> None:
    calls: list[str] = []

    async def start_a(app: FastAPI) -> None:
        calls.append("start_a")
        app.state.resource = "ready"

    async def start_b(app: FastAPI) -> None:
        calls.append(f"start_b:{app.state.resource}")

    async def stop_a(app: FastAPI) -> None:
        calls.append(f"stop_a:draining={app.state.draining}")

    async def stop_failing(_: FastAPI) -> None:
        calls.append("stop_failing")
        msg = "boom"
        raise RuntimeError(msg)

    async def stop_b(_: FastAPI) -> None:
        calls.append("stop_b")

    bus = InMemoryBus()
    app = make_test_app(
        bus=bus, on_startup=[start_a, start_b], on_shutdown=[stop_a, stop_failing, stop_b]
    )
    async with lifespan_client(app) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert calls == ["start_a", "start_b:ready"]
    assert calls == ["start_a", "start_b:ready", "stop_a:draining=True", "stop_failing", "stop_b"]
    assert not bus.is_connected  # closed after the hooks


async def test_slow_shutdown_hook_is_cut_off_by_grace_period() -> None:
    finished: list[str] = []

    async def hangs(_: FastAPI) -> None:
        await asyncio.sleep(10)
        finished.append("hangs")  # pragma: no cover - cancelled

    bus = InMemoryBus()
    app = make_test_app(
        bus=bus, settings=make_test_settings(shutdown_grace_seconds=0.05), on_shutdown=[hangs]
    )
    async with lifespan_client(app):
        pass
    assert finished == []
    assert not bus.is_connected


async def test_readyz_reports_draining_during_shutdown() -> None:
    statuses: list[int] = []
    app: FastAPI

    async def probe(_: FastAPI) -> None:
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            statuses.append((await client.get("/readyz")).status_code)

    app = make_test_app(on_shutdown=[probe])
    async with lifespan_client(app) as client:
        statuses.append((await client.get("/readyz")).status_code)
    assert statuses == [200, 503]


async def test_bus_dependency() -> None:
    bus = InMemoryBus()
    async with lifespan_client(make_test_app(routers=[router], bus=bus)) as client:
        response = await client.post(f"{API_PREFIX}/announce")
    assert response.json() == {"duplicate": False}
    assert [m.subject for m in bus.published] == ["event.test.announced"]


async def test_bus_dependency_without_bus_is_503() -> None:
    app = create_app(make_test_settings(), make_test_descriptor(), routers=[router])
    async with lifespan_client(app) as client:
        response = await client.post(f"{API_PREFIX}/announce")
        ready = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["type"] == "urn:mowdeck:problem:unavailable"
    assert "nats" not in ready.json()["checks"]


async def test_nats_enabled_connects_a_jetstream_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    async def fake_connect(self: JetStreamBus) -> None:
        events.append(f"connect:{self.servers}:{self.name}")

    async def fake_close(self: JetStreamBus) -> None:
        events.append("close")

    monkeypatch.setattr(JetStreamBus, "connect", fake_connect)
    monkeypatch.setattr(JetStreamBus, "close", fake_close)
    settings = make_test_settings(nats_enabled=True, nats_url="nats://n1:4222")
    app = create_app(settings, make_test_descriptor())
    async with lifespan_client(app) as client:
        assert isinstance(app.state.bus, JetStreamBus)
        ready = await client.get("/readyz")
    assert ready.status_code == 503  # fake bus never connected
    assert ready.json()["checks"]["nats"]["status"] == "fail"
    assert events == ["connect:['nats://n1:4222']:test-service", "close"]


async def test_bus_close_failure_is_logged_not_raised() -> None:
    class BrokenBus(InMemoryBus):
        async def close(self) -> None:
            msg = "close failed"
            raise RuntimeError(msg)

    async with lifespan_client(make_test_app(bus=BrokenBus())):
        pass


async def test_tracing_is_enabled_with_endpoint() -> None:
    settings = make_test_settings(otel_exporter_otlp_endpoint="http://127.0.0.1:4318/")
    app = make_test_app(settings=settings)
    assert getattr(app, "_is_instrumented_by_opentelemetry", False)
    async with lifespan_client(app):
        pass  # shutdown flushes the (empty) tracer provider
    assert traces_endpoint("http://c:4318/") == "http://c:4318/v1/traces"
    assert traces_endpoint("http://c:4318/v1/traces") == "http://c:4318/v1/traces"


def test_tracing_is_off_without_endpoint() -> None:
    app = make_test_app()
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)


def test_openapi_lives_under_api_prefix() -> None:
    app = make_test_app(routers=[router])
    assert app.openapi_url == f"{API_PREFIX}/openapi.json"
    paths = app.openapi()["paths"]
    assert f"{API_PREFIX}/announce" in paths
    assert "/healthz" not in paths


async def test_no_interactive_docs_from_a_cdn() -> None:
    # FastAPI's Swagger UI and ReDoc load their bundles from cdn.jsdelivr.net; the interactive
    # explorer is the web app's, self-hosted (ADR 0072). The OpenAPI document stays.
    app = make_test_app(routers=[router])
    assert app.docs_url is None
    assert app.redoc_url is None
    async with lifespan_client(app) as client:
        assert (await client.get(f"{API_PREFIX}/docs")).status_code == 404
        assert (await client.get(f"{API_PREFIX}/openapi.json")).status_code == 200


def test_run_uses_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(app: FastAPI, **kwargs: Any) -> None:
        captured.update(kwargs, app=app)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    settings = make_test_settings(http_port=8123, shutdown_grace_seconds=7)
    app = make_test_app(settings=settings)
    run(app, settings)
    assert captured["app"] is app
    assert captured["port"] == 8123
    assert captured["log_config"] is None
    assert captured["timeout_graceful_shutdown"] == 7


# --- db extra ---------------------------------------------------------------------------------


def test_async_database_url() -> None:
    assert async_database_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert async_database_url("postgres://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert async_database_url("postgresql+asyncpg://h/db") == "postgresql+asyncpg://h/db"
    with pytest.raises(ValueError, match="database URL"):
        async_database_url("not a url")


def test_create_engine_requires_url() -> None:
    with pytest.raises(ValueError, match="MOWDECK_DATABASE_URL"):
        create_engine(make_test_settings())


async def test_db_readiness_check_fails_without_server() -> None:
    settings = make_test_settings(
        database_url=SecretStr("postgresql://u:p@127.0.0.1:1/none"),
    )
    engine = create_engine(settings, pool_size=1)
    try:
        assert engine.url.drivername == "postgresql+asyncpg"
        outcome = await run_check(readiness_check_db(engine, timeout_seconds=5))
        assert outcome.name == "database"
        assert not outcome.ok
    finally:
        await engine.dispose()


async def test_session_dependency_commits_and_rolls_back() -> None:
    settings = make_test_settings(database_url=SecretStr("postgresql://u:p@127.0.0.1:1/none"))
    engine = create_engine(settings)
    get_session = session_dependency(engine)
    try:
        sessions = get_session()
        session = await anext(sessions)
        assert session.bind is engine
        with pytest.raises(StopAsyncIteration):
            await anext(sessions)  # commit of an unused session needs no connection

        failing = get_session()
        await anext(failing)
        with pytest.raises(RuntimeError):
            await failing.athrow(RuntimeError("handler failed"))
    finally:
        await engine.dispose()


DATABASE_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")


@pytest.mark.skipif(
    not DATABASE_URL,
    reason="needs MOWDECK_TEST_DATABASE_URL (e.g. postgresql://u:p@127.0.0.1:5433/db)",
)
async def test_db_against_real_postgres() -> None:
    from sqlalchemy import text

    settings = make_test_settings(database_url=SecretStr(DATABASE_URL or ""))
    engine = create_engine(settings)
    try:
        assert (await run_check(readiness_check_db(engine))).ok
        sessions = session_dependency(engine)()
        session = await anext(sessions)
        assert (await session.execute(text("SELECT 41 + 1"))).scalar_one() == 42
        with pytest.raises(StopAsyncIteration):
            await anext(sessions)
    finally:
        await engine.dispose()
