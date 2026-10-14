"""The mow path: snapshots on device.mower.<id>.path and preview requests (core NATS)."""

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from dg_support import (
    ISSUER,
    SlowMower,
    TestProvider,
    envelope,
    make_app,
    registration,
    telemetry,
    wait_until,
)
from mowdeck_contracts.v1 import (
    MowerPathSnapshot,
    MowerRegistration,
    MowPath,
    MowPathProgress,
    PathPreviewErrorCode,
    PathPreviewGatewayRequest,
    PathPreviewReply,
    ReturnToDock,
    StartMow,
)
from mowdeck_contracts.v1.subjects import command_subject, device_subject, path_preview_subject
from mowdeck_control import MowerConnection, PathUnavailable, SimulationConfig
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.domain.rules import path_geometry_key, path_publish_due

AT = datetime(2026, 10, 11, 9, tzinfo=UTC)


def _paths(bus: InMemoryBus, reg: MowerRegistration) -> list[MowerPathSnapshot]:
    subject = device_subject(reg.mower_id, "path")
    return [m.model(MowerPathSnapshot) for m in bus.messages(subject)]


async def _ask(bus: InMemoryBus, request: PathPreviewGatewayRequest) -> PathPreviewReply:
    reply = await bus.request(path_preview_subject(request.mower_id), request, timeout=2.0)
    return reply.model(PathPreviewReply)


def _request(reg: MowerRegistration, **changes: object) -> PathPreviewGatewayRequest:
    values: dict[str, object] = {
        "mower_id": reg.mower_id,
        "account_id": reg.account_id,
        "area_ids": ["front"],
        "pattern": {"name": "stripes"},
        "requested_by": ISSUER,
    }
    values.update(changes)
    return PathPreviewGatewayRequest.model_validate(values)


# --- snapshots ---------------------------------------------------------------------------------


async def test_dg_path_snapshot_follows_the_job_and_is_cleared_at_its_end() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(
        bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")], path_progress_seconds=0.01
    )
    async with lifespan_client(app):
        # the first check of a connection publishes, also "no path" (a stale value must go)
        await wait_until(lambda: _paths(bus, reg))
        assert _paths(bus, reg)[0].path is None
        await bus.publish(
            command_subject(reg.mower_id), envelope(reg, StartMow(area_ids=["front"]))
        )
        await wait_until(lambda: any(s.path is not None for s in _paths(bus, reg)))
        first = next(s.path for s in _paths(bus, reg) if s.path is not None)
        assert first.area_ids == ["front"]
        assert first.progress is not None
        await wait_until(
            lambda: any(
                s.path is not None and s.path.progress is not None and s.path.progress.index > 2
                for s in _paths(bus, reg)
            )
        )
        await bus.publish(command_subject(reg.mower_id), envelope(reg, ReturnToDock(end_job=True)))
        await wait_until(lambda: _paths(bus, reg)[-1].path is None, within=5.0)
    snaps = _paths(bus, reg)
    assert all(s.account_id == reg.account_id for s in snaps)
    msg_ids = [m.msg_id for m in bus.messages(device_subject(reg.mower_id, "path"))]
    assert len(set(msg_ids)) == len(msg_ids)  # two clears are never dropped as duplicates


async def test_dg_path_progress_is_throttled() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(
        bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")], path_progress_seconds=600
    )
    async with lifespan_client(app):
        await wait_until(lambda: _paths(bus, reg))
        await bus.publish(
            command_subject(reg.mower_id), envelope(reg, StartMow(area_ids=["front"]))
        )
        await wait_until(lambda: any(s.path is not None for s in _paths(bus, reg)))
        await asyncio.sleep(0.4)
        with_path = [s for s in _paths(bus, reg) if s.path is not None]
    assert len(with_path) == 1  # the route once; progress waits for the interval


def _path(index: int | None = None, path_hash: str = "1") -> MowPath:
    return MowPath(
        path_hash=path_hash,
        polylines=[[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]],
        points_total=3,
        progress=MowPathProgress(index=index) if index is not None else None,
        planned_at=AT,
    )


def test_path_publish_rules() -> None:
    assert path_publish_due(None, None, since_last_seconds=0, progress_interval_seconds=5)
    published = (path_geometry_key(_path(1)), _path(1).progress.model_dump_json())  # type: ignore[union-attr]
    same = {"since_last_seconds": 0.0, "progress_interval_seconds": 5.0}
    assert not path_publish_due(published, _path(1), **same)
    assert not path_publish_due(published, _path(2), **same)  # progress only: too soon
    assert path_publish_due(
        published, _path(2), since_last_seconds=5.0, progress_interval_seconds=5.0
    )
    assert path_publish_due(published, _path(1, path_hash="2"), **same)  # a new route
    assert path_publish_due(published, None, **same)  # cleared
    assert not path_publish_due(("none", ""), None, **same)


# --- preview -----------------------------------------------------------------------------------


async def test_dg_path_preview_of_a_simulated_mower() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        reply = await _ask(bus, _request(reg))
        assert reply.path is not None
        assert reply.path.progress is None
        assert reply.path.points_total > 0
        forbidden = await _ask(bus, _request(reg, account_id=uuid4()))
        assert forbidden.error is not None
        assert forbidden.error.code is PathPreviewErrorCode.FORBIDDEN
        unknown = await _ask(bus, _request(registration()))
        assert unknown.error is not None
        assert unknown.error.code is PathPreviewErrorCode.NOT_FOUND
        bad = await _ask(bus, _request(reg, area_ids=["nowhere"]))
        assert bad.error is not None
        assert bad.error.code is PathPreviewErrorCode.REJECTED
        assert "unknown area ids" in bad.error.detail
        await bus.publish(
            command_subject(reg.mower_id), envelope(reg, StartMow(area_ids=["front"]))
        )
        await wait_until(
            lambda: any(s.activity.value == "mowing" for s in telemetry(bus, reg.mower_id))
        )
        busy = await _ask(bus, _request(reg))
        assert busy.error is not None
        assert busy.error.code is PathPreviewErrorCode.REJECTED


class NoPreview(SlowMower):
    preview_path = None  # type: ignore[assignment]


class LatePreview(SlowMower):
    """Plans forever (the gateway's timeout ends the wait) or reports a missing route."""

    def __init__(self, *, timed_out: bool | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.timed_out = timed_out

    async def preview_path(
        self, area_ids: Sequence[str], pattern: Mapping[str, Any] | None
    ) -> MowPath:
        if self.timed_out is None:
            await asyncio.sleep(10)
        raise PathUnavailable("no route", timed_out=bool(self.timed_out))


@pytest.mark.parametrize(
    ("factory", "code"),
    [
        (lambda r: NoPreview(mower_id=r.mower_id, account_id=r.account_id), "unsupported"),
        (
            lambda r: LatePreview(timed_out=None, mower_id=r.mower_id, account_id=r.account_id),
            "timeout",
        ),
        (
            lambda r: LatePreview(timed_out=True, mower_id=r.mower_id, account_id=r.account_id),
            "timeout",
        ),
        (
            lambda r: LatePreview(timed_out=False, mower_id=r.mower_id, account_id=r.account_id),
            "failed",
        ),
    ],
)
async def test_dg_path_preview_errors(factory: Any, code: str) -> None:
    bus = InMemoryBus()
    reg = registration()

    def make(r: MowerRegistration) -> MowerConnection:
        connection: MowerConnection = factory(r)
        assert isinstance(connection, SlowMower)
        connection.config = SimulationConfig(time_scale=60)
        return connection

    provider = TestProvider({reg.mower_id: make})
    app = make_app(
        bus=bus,
        provider=provider,
        bootstrap_mowers=[reg.model_dump(mode="json")],
        path_preview_timeout_seconds=0.2,
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        reply = await _ask(bus, _request(reg))
    assert reply.error is not None
    assert reply.error.code.value == code
