# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The HTTP provisioner of the demo sandboxes (ADR 0076) against a fake of the other services."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from mowdeck_service_kit.demo import DEMO_SEED_HEADER

from identity.adapters.demo_sandbox import (
    MOWERS,
    PURGE_ORDER,
    HttpSandboxProvisioner,
    ProvisioningFailed,
)

SEED = "seed-secret"


class FakeServices:
    """Answers like Fleet, Scheduler, Notifications and Telemetry; records every call."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, str], object]] = []
        self.fail = fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, dict(request.headers), body))
        path = request.url.path
        if self.fail and self.fail in str(request.url):
            return httpx.Response(503, json={"title": "down"})
        if path.endswith("/readyz"):
            return httpx.Response(200)
        if path == "/api/v1/fleet/mowers":
            return httpx.Response(201, json={"id": str(uuid4())})
        if path == "/api/v1/notifications/preferences" and request.method == "GET":
            fields = ("enabled_kinds", "all_mowers", "mower_ids", "quiet_hours")
            more = ("timezone", "language", "channels")
            return httpx.Response(200, json={"stored": False, **dict.fromkeys(fields + more)})
        if "/demo/accounts/" in path:
            return httpx.Response(200, json={"deleted": 3})
        return httpx.Response(200, json={})


def _provisioner(fake: FakeServices) -> HttpSandboxProvisioner:
    return HttpSandboxProvisioner(
        seed_token=SEED, history_days=2, transport=httpx.MockTransport(fake)
    )


async def test_a_pool_sandbox_gets_mowers_schedules_preferences_and_history() -> None:
    fake = FakeServices()
    account_id = uuid4()
    await _provisioner(fake).provision(account_id, "visitor-token", history=True)
    calls = [(method, path) for method, path, _, _ in fake.calls]
    assert sorted(calls) == sorted(
        [
            *[("POST", "/api/v1/fleet/mowers")] * len(MOWERS),
            *[("POST", "/api/v1/scheduler/schedules")] * len(MOWERS),
            ("GET", "/api/v1/notifications/preferences"),
            ("PUT", "/api/v1/notifications/preferences"),
            ("POST", "/api/v1/telemetry/demo/history"),
        ]
    )
    # The mowers first (the schedules name them), the preferences read before they are
    # written, the history last (Telemetry needs the mowers).
    assert calls[: len(MOWERS)] == [("POST", "/api/v1/fleet/mowers")] * len(MOWERS)
    get = calls.index(("GET", "/api/v1/notifications/preferences"))
    assert get < calls.index(("PUT", "/api/v1/notifications/preferences"))
    assert calls[-1] == ("POST", "/api/v1/telemetry/demo/history")
    for method, path, headers, _ in fake.calls:
        assert headers["authorization"] == "Bearer visitor-token"
        seeded = path in ("/api/v1/fleet/mowers", "/api/v1/telemetry/demo/history")
        assert (headers.get(DEMO_SEED_HEADER.lower()) == SEED) is seeded, (method, path)
    history = fake.calls[-1][3]
    assert isinstance(history, dict)
    assert history["days"] == 2
    assert {m["account_id"] for m in history["mowers"]} == {str(account_id)}


class SlowServices(httpx.AsyncBaseTransport):
    """:class:`FakeServices` answering after a pause; counts the calls in flight at once."""

    def __init__(self) -> None:
        self.fake = FakeServices()
        self.in_flight = 0
        self.most = 0
        self.rounds: list[int] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.in_flight += 1
        self.most = max(self.most, self.in_flight)
        try:
            await asyncio.sleep(0.02)
            await request.aread()
            return self.fake(request)
        finally:
            self.in_flight -= 1


async def test_independent_calls_run_side_by_side_on_one_client() -> None:
    slow = SlowServices()
    provisioner = HttpSandboxProvisioner(seed_token=SEED, history_days=0, transport=slow)
    client = provisioner._shared()
    await provisioner.provision(uuid4(), "t", history=False)
    await provisioner.purge("fleet", uuid4())
    assert provisioner._shared() is client  # one client, its connections kept
    # Eight calls of the provisioning in four rounds (the first mower, the other two, the
    # schedules beside the preferences read, the preferences written), then the purge.
    assert len(slow.fake.calls) == 9
    assert slow.most == len(MOWERS) + 1  # three schedules and the preferences at once
    await provisioner.aclose()


async def test_a_failing_call_among_parallel_ones_fails_the_provisioning() -> None:
    fake = FakeServices(fail="scheduler")
    provisioner = HttpSandboxProvisioner(seed_token=SEED, transport=httpx.MockTransport(fake))
    with pytest.raises(ProvisioningFailed, match="scheduler"):
        await provisioner.provision(uuid4(), "t", history=True)
    assert all("history" not in path for _, path, _, _ in fake.calls)


async def test_a_sandbox_made_on_the_spot_has_no_history() -> None:
    fake = FakeServices()
    await _provisioner(fake).provision(uuid4(), "t", history=False)
    assert all("history" not in path for _, path, _, _ in fake.calls)


async def test_a_refusing_service_fails_the_provisioning() -> None:
    with pytest.raises(ProvisioningFailed, match="scheduler"):
        await _provisioner(FakeServices(fail="scheduler")).provision(uuid4(), "t", history=True)


async def test_the_purge_calls_the_service_with_the_seed_secret_only() -> None:
    fake = FakeServices()
    account_id = uuid4()
    provisioner = _provisioner(fake)
    for service in provisioner.services:
        await provisioner.purge(service, account_id)
    assert [path for _, path, _, _ in fake.calls] == [
        f"/api/v1/{service}/demo/accounts/{account_id}" for service in PURGE_ORDER
    ]
    for method, _, headers, _ in fake.calls:
        assert method == "DELETE"
        assert headers[DEMO_SEED_HEADER.lower()] == SEED
        assert "authorization" not in headers


async def test_a_failing_purge_raises_for_a_retry() -> None:
    with pytest.raises(ProvisioningFailed):
        await _provisioner(FakeServices(fail="telemetry")).purge("telemetry", uuid4())


async def test_ready_needs_every_service() -> None:
    assert await _provisioner(FakeServices()).ready()
    assert not await _provisioner(FakeServices(fail="vision")).ready()


def test_the_provisioner_needs_the_seed_secret() -> None:
    with pytest.raises(ValueError, match="seed"):
        HttpSandboxProvisioner(seed_token="")
