"""The health model (ADR 0077): liveness, started and readiness are three different answers.

A busy service or a dependency that fails after the start must never take a running container
out of routing (``/startupz``), while ``/readyz`` and the metrics keep reporting the
dependencies.
"""

import asyncio
import json
import logging
import os

import pytest
from mowdeck_service_kit.db import create_engine, readiness_check_db
from mowdeck_service_kit.health import ReadinessCheck, evaluate_readiness, run_check
from mowdeck_service_kit.logging import configure_logging
from mowdeck_service_kit.settings import ServiceSettings
from mowdeck_service_kit.testing import lifespan_client, make_test_app, make_test_settings
from pydantic import SecretStr


class Switch:
    """A readiness check whose outcome the test flips, counting its calls."""

    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return self.ok


def _settings(**overrides: object) -> ServiceSettings:
    return make_test_settings(readiness_interval_seconds=0, **overrides)


async def test_startupz_needs_one_ready_evaluation_then_stays_up() -> None:
    dependency = Switch(ok=False)
    app = make_test_app(
        settings=_settings(),
        readiness_checks=[ReadinessCheck(name="dependency", check=dependency)],
    )
    async with lifespan_client(app) as client:
        starting = await client.get("/startupz")
        assert starting.status_code == 503
        assert starting.json()["status"] == "starting"
        assert starting.json()["checks"]["dependency"]["error"] == "check returned false"
        assert (await client.get("/healthz")).status_code == 200  # alive all along

        dependency.ok = True
        assert (await client.get("/startupz")).json() == {"status": "ok"}
        calls = dependency.calls

        # Later trouble shows in /readyz, never in /startupz: no dependency call any more.
        dependency.ok = False
        assert (await client.get("/startupz")).status_code == 200
        assert dependency.calls == calls
        assert (await client.get("/readyz")).status_code == 503
        assert (await client.get("/startupz")).status_code == 200
        assert (await client.get("/healthz")).status_code == 200


async def test_startupz_answers_while_the_service_is_saturated() -> None:
    """A dependency that hangs (a pool with no free connection) delays only /readyz."""
    release = asyncio.Event()

    async def hanging() -> None:
        await release.wait()

    app = make_test_app(settings=_settings())
    async with lifespan_client(app) as client:
        assert (await client.get("/startupz")).status_code == 200
        app.state.readiness_checks.append(
            ReadinessCheck(name="pool", check=hanging, timeout_seconds=0.05)
        )
        started = asyncio.get_running_loop().time()
        assert (await client.get("/startupz")).status_code == 200
        assert (await client.get("/healthz")).status_code == 200
        assert asyncio.get_running_loop().time() - started < 0.05
        ready = await client.get("/readyz")
        release.set()
    assert ready.status_code == 503
    assert ready.json()["checks"]["pool"]["error"] == "timeout"


async def test_startupz_is_not_up_while_draining_before_the_start() -> None:
    app = make_test_app(
        settings=_settings(),
        readiness_checks=[ReadinessCheck(name="dependency", check=Switch(ok=False))],
    )
    async with lifespan_client(app) as client:
        app.state.draining = True
        response = await client.get("/startupz")
    assert response.status_code == 503
    assert response.json()["status"] == "draining"


async def test_the_watcher_marks_the_start_and_keeps_the_metrics_current(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dependency = Switch(ok=True)
    app = make_test_app(
        settings=make_test_settings(readiness_interval_seconds=0.01),
        readiness_checks=[ReadinessCheck(name="dependency", check=dependency)],
    )
    async with lifespan_client(app) as client:
        for _ in range(200):
            if app.state.started:
                break
            await asyncio.sleep(0.01)
        assert app.state.started
        assert (await client.get("/startupz")).status_code == 200
        metrics = (await client.get("/metrics")).text
        assert 'mowdeck_readiness_check_up{check="dependency"} 1.0' in metrics
        assert "mowdeck_service_started 1.0" in metrics

        dependency.ok = False
        for _ in range(200):
            if app.state.last_ready is False:
                break
            await asyncio.sleep(0.01)
        metrics = (await client.get("/metrics")).text
        assert 'mowdeck_readiness_check_up{check="dependency"} 0.0' in metrics
        assert "mowdeck_service_started 1.0" in metrics
        assert (await client.get("/startupz")).status_code == 200
    calls = dependency.calls
    await asyncio.sleep(0.05)
    assert dependency.calls == calls, "the watcher stops with the service"


async def test_evaluate_readiness_logs_only_changes(capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_test_settings(log_format="json", readiness_interval_seconds=0)
    dependency = Switch(ok=True)
    app = make_test_app(
        settings=settings, readiness_checks=[ReadinessCheck(name="dependency", check=dependency)]
    )
    async with lifespan_client(app):
        capsys.readouterr()
        await evaluate_readiness(app)
        dependency.ok = False
        await evaluate_readiness(app)
        await evaluate_readiness(app)
        dependency.ok = True
        await evaluate_readiness(app)
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    events = [line["event"] for line in lines if "ready" in line["event"]]
    assert events == ["service not ready", "service ready again"]
    failing = next(line for line in lines if line["event"] == "service not ready")
    assert failing["failing"] == ["dependency: check returned false"]


async def test_database_readiness_does_not_use_the_request_pool() -> None:
    """Without a server it fails like before; the probe never touches the request pool."""
    settings = make_test_settings(
        database_url=SecretStr("postgresql://u:p@127.0.0.1:1/none"),
        database_pool_size=1,
        database_max_overflow=0,
    )
    engine = create_engine(settings)
    try:
        assert engine.pool.size() == 1  # type: ignore[attr-defined]
        outcome = await run_check(readiness_check_db(engine, timeout_seconds=5))
        assert not outcome.ok
        assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    finally:
        await engine.dispose()


DATABASE_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")


@pytest.mark.skipif(not DATABASE_URL, reason="needs MOWDECK_TEST_DATABASE_URL")
async def test_database_readiness_passes_while_every_pooled_connection_is_busy() -> None:
    from sqlalchemy import text

    settings = make_test_settings(
        database_url=SecretStr(DATABASE_URL or ""), database_pool_size=1, database_max_overflow=0
    )
    engine = create_engine(settings, pool_timeout=0.2)
    try:
        async with engine.connect() as busy:
            await busy.execute(text("SELECT 1"))
            assert (await run_check(readiness_check_db(engine, timeout_seconds=2))).ok
    finally:
        await engine.dispose()


def test_pool_settings_have_a_bounded_default() -> None:
    settings = make_test_settings()
    assert (settings.database_pool_size, settings.database_max_overflow) == (5, 5)
    engine = create_engine(
        make_test_settings(
            database_url=SecretStr("postgresql://u:p@127.0.0.1:1/none"),
            database_pool_size=7,
            database_max_overflow=2,
        )
    )
    assert engine.pool.size() == 7  # type: ignore[attr-defined]
    assert engine.pool._max_overflow == 2  # type: ignore[attr-defined]


def test_stdlib_extra_fields_reach_the_json_log(capsys: pytest.CaptureFixture[str]) -> None:
    """Libraries log with ``extra``: the cause of a denied authorization check must be visible
    (docs/ops/load-testing.md), secrets still masked."""
    configure_logging(make_test_settings(log_format="json"))
    logging.getLogger("mowdeck_authz.openfga").warning(
        "authorization check failed; denying (fail closed)",
        extra={"operation": "check", "error": "TimeoutError", "api_token": "s3cr3t"},
    )
    logging.getLogger("uvicorn.error").info("started", extra={"color_message": "started"})
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    denied = next(line for line in lines if line["event"].startswith("authorization check"))
    assert denied["operation"] == "check"
    assert denied["error"] == "TimeoutError"
    assert denied["api_token"] == "***"  # noqa: S105 - the mask, not a password
    assert "s3cr3t" not in json.dumps(lines)
    started = next(line for line in lines if line["event"] == "started")
    assert "color_message" not in started
