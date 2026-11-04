# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The sandbox cost script's lifecycle helpers (ADR 0079): percentiles, the sandbox counts and
one visitor's claim and logout against a fake gateway."""

import asyncio
import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest


@pytest.fixture(scope="module")
def cost(repo_root: Path) -> ModuleType:
    path = repo_root / "tools/repo-checks/scripts/demo_sandbox_cost.py"
    spec = importlib.util.spec_from_file_location("demo_sandbox_cost", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_percentiles(cost: ModuleType) -> None:
    assert cost.percentiles([]) == "-"
    values = [i / 1000 for i in range(1, 101)]  # 1 ms .. 100 ms
    assert cost.percentiles(values) == "median 50 ms, p95 95 ms, max 100 ms"


def test_sandbox_states_parses_the_registry(
    cost: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "ready,8\nactive,3\nnoise\n", "")

    monkeypatch.setattr(cost.subprocess, "run", run)
    assert cost.sandbox_states("mowdeck-load") == {"ready": 8, "active": 3}
    assert seen[0][:3] == ["docker", "exec", "mowdeck-load-postgres-1"]


def test_a_visitor_claims_looks_and_logs_out_from_its_own_address(
    cost: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, str | None]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("x-forwarded-for")))
        path = request.url.path
        if path == "/api/v1/identity/demo/session":
            return httpx.Response(200, json={"access_token": "a", "refresh_token": "r"})
        if path == "/api/v1/fleet/mowers":
            return httpx.Response(200, json={"items": [{"id": "m1"}]})
        if path == "/api/v1/identity/session/logout":
            return httpx.Response(204)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(gateway)
    real = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real(*args, transport=transport, **kwargs)

    monkeypatch.setattr(cost.httpx, "AsyncClient", client)
    visitor = cost.Claimed()

    async def run() -> None:
        await cost.claim_and_look("http://gw", "198.18.0.1", 0, visitor)
        start = asyncio.Event()
        start.set()
        await cost.log_out("http://gw", "198.18.0.1", visitor, start)

    asyncio.run(run())
    assert visitor.errors == []
    assert visitor.claim is not None and visitor.first_view is not None
    assert visitor.logout is not None
    paths = [path for _, path, _ in calls]
    assert paths[0] == "/config.json"  # the connection is opened before the measured request
    assert "/api/v1/telemetry/mowers/m1/state" in paths
    assert paths[-1] == "/api/v1/identity/session/logout"
    assert {address for _, _, address in calls} == {"198.18.0.1"}
