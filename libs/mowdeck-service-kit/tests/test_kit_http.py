"""HTTP behaviour: correlation ids, logging, problem+json errors, health endpoints."""

import json
from typing import Any

import pytest
from fastapi import APIRouter
from mowdeck_contracts.v1 import ProblemFieldError
from mowdeck_service_kit import (
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    Unauthorized,
    ValidationFailed,
    get_logger,
)
from mowdeck_service_kit.correlation import (
    CORRELATION_HEADER,
    correlation_scope,
    get_correlation_id,
    is_valid_correlation_id,
)
from mowdeck_service_kit.errors import field_errors_from_validation
from mowdeck_service_kit.health import ReadinessCheck, add_readiness_check
from mowdeck_service_kit.logging import configure_logging
from mowdeck_service_kit.testing import (
    lifespan_client,
    make_test_app,
    make_test_descriptor,
    make_test_settings,
)
from pydantic import BaseModel, Field

PROBLEM = "application/problem+json"

router = APIRouter()
_log = get_logger("tests")


class Item(BaseModel):
    name: str = Field(min_length=1)
    count: int = Field(ge=0)


@router.get("/echo-correlation")
async def echo_correlation() -> dict[str, Any]:
    _log.info("handling echo")
    return {"correlation_id": get_correlation_id()}


@router.post("/items")
async def create_item(item: Item, limit: int = 10) -> Item:
    return item


ERRORS: dict[str, ServiceError] = {
    "bad": BadRequest("bad input"),
    "unauthorized": Unauthorized(),
    "forbidden": Forbidden("nope"),
    "notfound": NotFound("no such mower"),
    "conflict": Conflict("already exists"),
    "invalid": ValidationFailed(
        "rule violated", errors=[ProblemFieldError(detail="too big", pointer="#/count")]
    ),
    "unavailable": ServiceUnavailable(),
}


@router.get("/raise/{kind}")
async def raise_error(kind: str) -> None:
    raise ERRORS[kind]


@router.get("/crash")
async def crash() -> None:
    msg = "database password is hunter2"
    raise RuntimeError(msg)


async def test_correlation_id_is_generated_and_returned() -> None:
    async with lifespan_client(make_test_app(routers=[router])) as client:
        response = await client.get("/api/v1/echo-correlation")
    returned = response.headers[CORRELATION_HEADER]
    assert is_valid_correlation_id(returned)
    assert response.json()["correlation_id"] == returned
    assert get_correlation_id() is None  # reset after the request


@pytest.mark.parametrize("header", ["X-Correlation-ID", "X-Request-ID"])
async def test_incoming_correlation_id_is_kept(header: str) -> None:
    async with lifespan_client(make_test_app(routers=[router])) as client:
        response = await client.get("/api/v1/echo-correlation", headers={header: "abc-123"})
    assert response.headers[CORRELATION_HEADER] == "abc-123"
    assert response.json()["correlation_id"] == "abc-123"


async def test_malformed_correlation_id_is_replaced() -> None:
    async with lifespan_client(make_test_app(routers=[router])) as client:
        response = await client.get(
            "/api/v1/echo-correlation", headers={CORRELATION_HEADER: "bad id;" * 30}
        )
    assert response.headers[CORRELATION_HEADER] != "bad id;" * 30
    assert is_valid_correlation_id(response.headers[CORRELATION_HEADER])


async def test_correlation_id_appears_in_json_logs(capsys: pytest.CaptureFixture[str]) -> None:
    app = make_test_app(routers=[router], settings=make_test_settings(log_format="json"))
    async with lifespan_client(app) as client:
        await client.get("/api/v1/echo-correlation", headers={CORRELATION_HEADER: "log-me"})
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    handled = [line for line in lines if line.get("event") == "handling echo"]
    assert handled, lines
    assert handled[0]["correlation_id"] == "log-me"
    assert handled[0]["service"] == "test-service"
    assert handled[0]["level"] == "info"
    assert handled[0]["timestamp"].endswith("Z")


def test_console_logging_and_scope(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_test_settings(log_format="console", log_level="debug"))
    with correlation_scope("scoped-1") as cid:
        get_logger("tests").debug("console line")
    assert cid == "scoped-1"
    out = capsys.readouterr().out
    assert "console line" in out
    assert "scoped-1" in out
    with correlation_scope("not valid!") as generated:
        assert generated != "not valid!"


@pytest.mark.parametrize(
    ("kind", "status", "problem_type"),
    [
        ("bad", 400, "urn:mowdeck:problem:bad-request"),
        ("unauthorized", 401, "urn:mowdeck:problem:unauthorized"),
        ("forbidden", 403, "urn:mowdeck:problem:forbidden"),
        ("notfound", 404, "urn:mowdeck:problem:not-found"),
        ("conflict", 409, "urn:mowdeck:problem:conflict"),
        ("invalid", 422, "urn:mowdeck:problem:validation-failed"),
        ("unavailable", 503, "urn:mowdeck:problem:unavailable"),
    ],
)
async def test_service_errors_render_problem_json(
    kind: str, status: int, problem_type: str
) -> None:
    async with lifespan_client(make_test_app(routers=[router])) as client:
        response = await client.get(f"/api/v1/raise/{kind}")
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert body["type"] == problem_type
    assert body["status"] == status
    assert body["title"] == ERRORS[kind].title
    assert body["instance"] == f"/api/v1/raise/{kind}"
    assert body["correlation_id"] == response.headers[CORRELATION_HEADER]
    if kind == "unauthorized":
        assert response.headers["www-authenticate"] == "Bearer"
    if kind == "invalid":
        assert body["errors"] == [{"detail": "too big", "pointer": "#/count"}]


async def test_request_validation_errors() -> None:
    async with lifespan_client(make_test_app(routers=[router])) as client:
        response = await client.post(
            "/api/v1/items?limit=abc", json={"name": "", "count": -1, "secret": "x"}
        )
    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert body["type"] == "urn:mowdeck:problem:validation-failed"
    pointers = {e.get("pointer") for e in body["errors"]}
    parameters = {e.get("parameter") for e in body["errors"]}
    assert {"#/name", "#/count"} <= pointers
    assert "limit" in parameters
    assert all("input" not in e for e in body["errors"])


def test_field_errors_cover_all_locations() -> None:
    errors = field_errors_from_validation(
        [
            {"loc": ("body",), "msg": "invalid json", "type": "json_invalid"},
            {"loc": ("body", "a/b", 0, "c~d"), "msg": "m", "type": "t"},
            {"loc": ("header", "x-api"), "msg": "missing"},
            {"loc": (), "msg": "odd"},
        ]
    )
    assert errors[0].pointer == "#"
    assert errors[1].pointer == "#/a~1b/0/c~0d"
    assert errors[2].parameter == "x-api"
    assert errors[3].pointer is None
    assert errors[3].parameter is None


async def test_unknown_route_and_method_are_problems() -> None:
    async with lifespan_client(make_test_app(routers=[router])) as client:
        missing = await client.get("/api/v1/nope")
        wrong_method = await client.delete("/api/v1/items")
    assert missing.status_code == 404
    assert missing.headers["content-type"] == PROBLEM
    assert missing.json()["title"] == "Not found"
    assert wrong_method.status_code == 405
    assert wrong_method.json()["title"] == "Method not allowed"


async def test_unexpected_exception_is_500_without_internals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = make_test_app(routers=[router], settings=make_test_settings(log_format="json"))
    async with lifespan_client(app) as client:
        response = await client.get("/api/v1/crash", headers={CORRELATION_HEADER: "crash-1"})
    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM
    assert response.headers[CORRELATION_HEADER] == "crash-1"
    body = response.json()
    assert body["correlation_id"] == "crash-1"
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    errors = [line for line in logged if line.get("event") == "unhandled exception"]
    assert errors
    assert errors[0]["correlation_id"] == "crash-1"
    assert "RuntimeError" in errors[0]["exception"]


async def test_healthz_and_module_descriptor() -> None:
    descriptor = make_test_descriptor(name="fleet")
    async with lifespan_client(make_test_app(descriptor=descriptor)) as client:
        health = await client.get("/healthz")
        module = await client.get("/.well-known/mowdeck-module")
        not_versioned = await client.get("/api/v1/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert module.status_code == 200
    assert module.json() == descriptor.model_dump(mode="json")
    assert not_versioned.status_code == 404


async def test_readyz_ok_and_failures() -> None:
    async def ok() -> None:
        return None

    async def broken() -> None:
        msg = "connection refused to 10.0.0.1"
        raise ConnectionError(msg)

    async def slow() -> None:
        import asyncio

        await asyncio.sleep(5)

    async def negative() -> bool:
        return False

    app = make_test_app(readiness_checks=[ok])
    async with lifespan_client(app) as client:
        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"]["ok"]["status"] == "ok"
        assert ready.json()["checks"]["nats"]["status"] == "ok"  # in-memory bus

        add_readiness_check(app, broken)
        add_readiness_check(app, ReadinessCheck(name="slow", check=slow, timeout_seconds=0.05))
        add_readiness_check(app, negative)
        failing = await client.get("/readyz")
    assert failing.status_code == 503
    checks = failing.json()["checks"]
    assert failing.json()["status"] == "fail"
    assert checks["broken"] == {
        "status": "fail",
        "duration_ms": checks["broken"]["duration_ms"],
        "error": "ConnectionError",
    }
    assert checks["slow"]["error"] == "timeout"
    assert checks["negative"]["error"] == "check returned false"
    assert "10.0.0.1" not in failing.text


async def test_metrics_endpoint() -> None:
    async with lifespan_client(make_test_app(routers=[router])) as client:
        await client.get("/api/v1/echo-correlation")
        await client.get("/api/v1/nope")
        metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain")
    text = metrics.text
    assert 'http_requests_total{method="GET",route="/api/v1/echo-correlation",status="200"}' in text
    assert 'route="<unmatched>",status="404"' in text
    assert "http_request_duration_seconds_bucket" in text
    assert "process_" in text or "python_info" in text


async def test_metrics_can_be_disabled() -> None:
    app = make_test_app(settings=make_test_settings(metrics_enabled=False))
    async with lifespan_client(app) as client:
        response = await client.get("/metrics")
    assert response.status_code == 404
    assert app.state.metrics_registry is None
