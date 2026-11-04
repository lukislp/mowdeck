# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Demo sandboxes under bursts (ADR 0079): claims beyond the pool side by side under the cap,
the sweep soon after a logout and again when the settle time after Fleet is over."""

import asyncio
import dataclasses
from datetime import timedelta
from typing import Any

import pytest
from identity_support import API, harness
from test_identity_demo_sandboxes import REFRESH, SANDBOXES, FakeProvisioner, _enter, _rows

from identity.application.context import RequestContext
from identity.application.sandboxes import SandboxService
from identity.domain.errors import DemoFull
from identity.domain.models import DemoSandbox, SandboxState


def _service(h: Any) -> SandboxService:
    service: SandboxService | None = h.app.state.identity.sandboxes
    assert service is not None
    return service


class Overlap:
    """Wraps ``SandboxService._create``: counts the creations running at once."""

    def __init__(
        self, service: SandboxService, *, fail: bool = False, gate: asyncio.Event | None = None
    ) -> None:
        self.original = service._create
        self.running = 0
        self.most = 0
        self.fail = fail
        self.gate = gate
        """Sandboxes made for a visitor wait for it (pool sandboxes do not)."""

    async def __call__(self, *, claimed_at: Any) -> DemoSandbox:
        if claimed_at is None:
            return await self.original(claimed_at=claimed_at)
        self.running += 1
        self.most = max(self.most, self.running)
        try:
            await asyncio.sleep(0.01)  # the account's OpenFGA tuples take a while
            if self.gate is not None:
                await self.gate.wait()
            if self.fail:
                msg = "openfga is down"
                raise RuntimeError(msg)
            return await self.original(claimed_at=claimed_at)
        finally:
            self.running -= 1


async def _claim(service: SandboxService, n: int) -> object:
    try:
        return await service.enter(RequestContext(ip=f"203.0.113.{n}"))
    except DemoFull as exc:
        return exc


async def test_a_burst_beyond_the_pool_makes_sandboxes_side_by_side_under_the_cap() -> None:
    async with harness(
        provisioner=FakeProvisioner(), **{**SANDBOXES, "identity_demo_sandbox_max": 3}
    ) as h:
        service = _service(h)
        overlap = Overlap(service)
        service._create = overlap  # type: ignore[method-assign]
        results = await asyncio.gather(*(_claim(service, n) for n in range(6)))
        rows = await _rows(h)
    assert sum(1 for r in results if isinstance(r, DemoFull)) == 3
    assert overlap.most == 3  # not one after the other
    assert sorted(r.state for r in rows) == [SandboxState.ACTIVE] * 3
    assert service._reserved == 0


async def test_only_a_few_are_made_at_once_and_a_waiting_visitor_takes_a_refill() -> None:
    fake = FakeProvisioner()
    async with harness(provisioner=fake, **SANDBOXES) as h:
        service = _service(h)
        service.config = dataclasses.replace(service.config, pool_size=1)
        service._making = asyncio.Semaphore(1)
        gate = asyncio.Event()
        overlap = Overlap(service, gate=gate)
        service._create = overlap  # type: ignore[method-assign]
        first = asyncio.create_task(_claim(service, 1))
        second = asyncio.create_task(_claim(service, 2))
        await asyncio.sleep(0.05)  # the first is being made, the second waits for a turn
        assert service._reserved == 2
        assert await service.fill_pool() == 1  # the refill meanwhile
        gate.set()
        await asyncio.gather(first, second)
        rows = {r.account_id: r for r in await _rows(h)}
    assert overlap.most == 1
    assert service._reserved == 0
    assert sorted(r.state for r in rows.values()) == [SandboxState.ACTIVE] * 2
    with_history = {account for account, history in fake.provisioned if history}
    assert len(with_history) == 1  # the second visitor got the refilled pool sandbox
    assert rows[next(iter(with_history))].claimed_at is not None


async def test_a_failed_creation_gives_its_place_back() -> None:
    async with harness(
        provisioner=FakeProvisioner(), **{**SANDBOXES, "identity_demo_sandbox_max": 1}
    ) as h:
        service = _service(h)
        failing = Overlap(service, fail=True)
        service._create = failing  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await service.enter(RequestContext(ip="203.0.113.1"))
        service._create = failing.original  # type: ignore[method-assign]
        visitor = await _enter(h)
    assert service._reserved == 0
    assert visitor["me"]["username"].startswith("visitor-")


async def test_a_logout_wakes_the_sweep_and_the_settle_time_sets_the_next_one() -> None:
    fake = FakeProvisioner()
    async with harness(provisioner=fake, **SANDBOXES) as h:
        service = _service(h)
        service.config = dataclasses.replace(service.config, nudge_delay=timedelta(0))
        await _enter(h)
        assert await service.next_sweep_in(60.0) == 60.0  # nothing to purge
        sweeper = asyncio.create_task(service.run_sweep(3600.0))
        try:
            await asyncio.sleep(0.05)  # the first sweep ran, the next one is an hour away
            logout = await h.client.post(f"{API}/session/logout", headers=REFRESH)
            for _ in range(100):
                if fake.purged:
                    break
                await asyncio.sleep(0.02)
        finally:
            sweeper.cancel()
        rows = await _rows(h)
        wait = await service.next_sweep_in(60.0)
    assert logout.status_code == 204
    assert [name for name, _ in fake.purged] == ["fleet"]  # then the settle time
    assert rows[0].state is SandboxState.PURGING
    assert 1.0 <= wait <= service.config.settle.total_seconds()  # not the full interval
