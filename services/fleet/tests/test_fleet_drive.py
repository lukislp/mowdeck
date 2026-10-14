"""Manual drive sessions (ADR 0026): ``fleet.drive.start``/``stop`` and the audit API."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fleet_support import API, harness
from mowdeck_contracts.v1 import (
    DriveEndReason,
    DriveErrorCode,
    DriveStartReply,
    DriveStartRequest,
    DriveStopReply,
    DriveStopRequest,
    PrincipalKind,
    PrincipalRef,
    utc_now,
)
from mowdeck_contracts.v1.subjects import FLEET_DRIVE_START, FLEET_DRIVE_STOP
from mowdeck_service_kit.bus import InMemoryBus, RequestTimeoutError

from fleet.application.drive import drive_lock_key


def _start(
    subject: UUID,
    account_id: UUID,
    mower_id: UUID,
    *,
    kind: PrincipalKind = PrincipalKind.USER,
    max_seconds: int = 600,
) -> DriveStartRequest:
    return DriveStartRequest(
        on_behalf_of=PrincipalRef(subject=subject, kind=kind, display_name="Driver"),
        account_id=account_id,
        mower_id=mower_id,
        blade_on_allowed=True,
        max_seconds=max_seconds,
        connection_id=uuid4(),
    )


async def _ask(bus: InMemoryBus, request: DriveStartRequest) -> DriveStartReply:
    return (await bus.request(FLEET_DRIVE_START, request)).model(DriveStartReply)


async def _stop(
    bus: InMemoryBus,
    session_id: UUID,
    account_id: UUID,
    mower_id: UUID,
    reason: DriveEndReason = DriveEndReason.CLIENT_STOP,
) -> DriveStopReply:
    request = DriveStopRequest(
        session_id=session_id,
        account_id=account_id,
        mower_id=mower_id,
        reason=reason,
        ended_at=utc_now(),
        frames=42,
        frames_dropped=3,
    )
    return (await bus.request(FLEET_DRIVE_STOP, request)).model(DriveStopReply)


async def test_fleet_drive_start_stop_and_audit_round_trip() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = UUID((await h.register(owner))["id"])
        reply = await _ask(h.bus, _start(owner.principal.subject, owner.account_id, mower_id))
        assert reply.error is None and reply.started is not None
        started = reply.started
        assert started.account_id == owner.account_id
        assert started.mower_id == mower_id
        assert started.expires_at - started.started_at == timedelta(seconds=600)
        # One open session per mower.
        busy = await _ask(h.bus, _start(owner.principal.subject, owner.account_id, mower_id))
        assert busy.error is not None and busy.error.code is DriveErrorCode.BUSY
        listed = await h.client.get(
            f"{API}/mowers/{mower_id}/drive-sessions", headers=owner.headers
        )
        assert listed.status_code == 200
        [open_item] = listed.json()["items"]
        assert open_item["session_id"] == str(started.session_id)
        assert open_item["ended_at"] is None
        assert open_item["blade_on_allowed"] is True
        assert open_item["started_by"]["subject"] == str(owner.principal.subject)
        stopped = await _stop(h.bus, started.session_id, owner.account_id, mower_id)
        assert stopped.found
        again = await _stop(h.bus, started.session_id, owner.account_id, mower_id)
        assert not again.found  # closing twice is harmless
        listed = await h.client.get(
            f"{API}/mowers/{mower_id}/drive-sessions", headers=owner.headers
        )
        [closed] = listed.json()["items"]
        assert closed["end_reason"] == "client_stop"
        assert (closed["frames"], closed["frames_dropped"]) == (42, 3)
        # After the stop a new session may start.
        second = await _ask(h.bus, _start(owner.principal.subject, owner.account_id, mower_id))
        assert second.started is not None
        page = await h.client.get(
            f"{API}/mowers/{mower_id}/drive-sessions",
            headers=owner.headers,
            params={"limit": 1},
        )
        body = page.json()
        assert [i["session_id"] for i in body["items"]] == [str(second.started.session_id)]
        older = await h.client.get(
            f"{API}/mowers/{mower_id}/drive-sessions",
            headers=owner.headers,
            params={"before": body["next_before"]},
        )
        assert [i["session_id"] for i in older.json()["items"]] == [str(started.session_id)]


async def test_fleet_drive_checks_can_drive_for_the_subject() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = UUID((await h.register(owner))["id"])
        viewer = await h.member(owner, "viewer")
        operator = await h.member(owner, "operator")
        stranger = await h.owner()
        denied = await _ask(h.bus, _start(viewer.principal.subject, owner.account_id, mower_id))
        assert denied.error is not None and denied.error.code is DriveErrorCode.FORBIDDEN
        hidden = await _ask(
            h.bus, _start(stranger.principal.subject, stranger.account_id, mower_id)
        )
        assert hidden.error is not None and hidden.error.code is DriveErrorCode.NOT_FOUND
        # A subject of the right account named with another account sees nothing.
        wrong_account = await _ask(
            h.bus, _start(owner.principal.subject, stranger.account_id, mower_id)
        )
        assert wrong_account.error is not None
        assert wrong_account.error.code is DriveErrorCode.NOT_FOUND
        service = await _ask(
            h.bus,
            _start(uuid4(), owner.account_id, mower_id, kind=PrincipalKind.SERVICE),
        )
        assert service.error is not None and service.error.code is DriveErrorCode.INVALID
        allowed = await _ask(h.bus, _start(operator.principal.subject, owner.account_id, mower_id))
        assert allowed.started is not None
        # The audit is hidden from other accounts and needs can_view.
        foreign = await h.client.get(
            f"{API}/mowers/{mower_id}/drive-sessions", headers=stranger.headers
        )
        assert foreign.status_code == 404
        seen = await h.client.get(f"{API}/mowers/{mower_id}/drive-sessions", headers=viewer.headers)
        assert seen.status_code == 200
        # A stop naming another account changes nothing.
        stop = await _stop(h.bus, allowed.started.session_id, stranger.account_id, mower_id)
        assert not stop.found


async def test_fleet_drive_expired_open_session_is_closed_by_the_next_start() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = UUID((await h.register(owner))["id"])
        drive = h.app.state.fleet.drive
        first = await _ask(
            h.bus, _start(owner.principal.subject, owner.account_id, mower_id, max_seconds=5)
        )
        assert first.started is not None
        later = utc_now() + timedelta(seconds=30)
        drive.clock = lambda: later
        second = await _ask(h.bus, _start(owner.principal.subject, owner.account_id, mower_id))
        assert second.started is not None
        page = await drive.list_for_mower(owner.account_id, mower_id)
        by_id = {item.session_id: item for item in page.items}
        expired = by_id[first.started.session_id]
        assert expired.end_reason is DriveEndReason.EXPIRED
        assert expired.ended_at == first.started.expires_at
        assert by_id[second.started.session_id].ended_at is None
        # Retention: everything that started before the cutoff goes.
        drive.clock = lambda: later + timedelta(days=100)
        assert await drive.purge(timedelta(days=90)) == 2
        with pytest.raises(Exception, match="mower not found"):
            await drive.list_for_mower(owner.account_id, uuid4())


async def test_fleet_drive_invalid_requests_are_dropped() -> None:
    async with harness() as h:
        with pytest.raises(RequestTimeoutError):
            await h.bus.request(FLEET_DRIVE_START, b"{}", timeout=0.2)
        with pytest.raises(RequestTimeoutError):
            await h.bus.request(FLEET_DRIVE_STOP, b"not json", timeout=0.2)


def test_fleet_drive_lock_key_is_a_signed_64_bit_value() -> None:
    for _ in range(50):
        key = drive_lock_key(uuid4())
        assert -(1 << 63) <= key < (1 << 63)
    mower_id = uuid4()
    assert drive_lock_key(mower_id) == drive_lock_key(mower_id)


async def test_fleet_drive_records_the_origin_of_an_automated_session() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = UUID((await h.register(owner))["id"])
        request = _start(owner.principal.subject, owner.account_id, mower_id).model_copy(
            update={"origin": "vision", "blade_on_allowed": False, "connection_id": None}
        )
        reply = await _ask(h.bus, request)
        assert reply.started is not None
        stopped = await _stop(
            h.bus, reply.started.session_id, owner.account_id, mower_id, DriveEndReason.COMPLETED
        )
        assert stopped.found
        listed = await h.client.get(
            f"{API}/mowers/{mower_id}/drive-sessions", headers=owner.headers
        )
        [item] = listed.json()["items"]
        assert item["origin"] == "vision"
        assert item["end_reason"] == "completed"
        assert item["blade_on_allowed"] is False
