"""Use cases with fake ports: replies, effects carried out, and account isolation."""

from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1.camera import CameraProvider, CameraSession
from mowdeck_contracts.v1.relay import (
    CAMERA_RELAY_LEASE_CLOSED,
    CAMERA_RELAY_LEASE_OPENED,
    CAMERA_RELAY_STARTED,
    CAMERA_RELAY_STOPPED,
    RelayDetachRequest,
    RelayErrorCode,
    RelayPurpose,
    RelayRenewRequest,
)
from mowdeck_contracts.v1.subjects import parse_camera_relay_event_subject
from relay_support import ONLINE, Harness, at, attach_request, key, principal

from camera_relay.application.ports import SourceUnavailableError, UnsupportedProviderError
from camera_relay.domain.models import RelayKey, SessionState
from camera_relay.domain.policy import MowerStatus
from camera_relay.domain.tickets import camera_path


async def _attach(
    h: Harness, relay_key: RelayKey, cameras: list[str] | None = None
) -> CameraSession:
    reply = await h.service.attach(attach_request(relay_key, cameras))
    assert reply.session is not None, reply.error
    assert reply.session.relay is not None
    return reply.session


def _renew(
    relay_key: RelayKey,
    lease_id: UUID,
    account_id: UUID | None = None,
    mower_id: UUID | None = None,
) -> RelayRenewRequest:
    return RelayRenewRequest(
        account_id=account_id or relay_key.account_id,
        mower_id=mower_id or relay_key.mower_id,
        lease_id=lease_id,
        requested_by=principal(),
    )


def _detach(
    relay_key: RelayKey,
    lease_id: UUID,
    account_id: UUID | None = None,
    mower_id: UUID | None = None,
) -> RelayDetachRequest:
    return RelayDetachRequest(
        account_id=account_id or relay_key.account_id,
        mower_id=mower_id or relay_key.mower_id,
        lease_id=lease_id,
        requested_by=principal(),
    )


async def _go_live(h: Harness, relay_key: RelayKey) -> None:
    session = h.repository.get(relay_key)
    assert session is not None and session.session_id is not None
    await h.service.source_started(
        relay_key, session.session_id, CameraProvider.FAKE, ["front_left"]
    )


async def test_attach_returns_a_relay_session_with_a_valid_ticket() -> None:
    h = Harness()
    relay_key = key()
    camera_session = await _attach(h, relay_key, ["front_left", "front_right"])
    relay = camera_session.relay
    assert relay is not None
    assert camera_session.provider is CameraProvider.RELAY
    assert (camera_session.token, camera_session.app_id) == ("", "")
    session = h.repository.get(relay_key)
    assert session is not None and session.relay_id is not None
    assert [t.whep_url for t in relay.tiles] == [
        f"/whep/{camera_path(session.relay_id, c)}/whep" for c in ("front_left", "front_right")
    ]
    assert str(relay_key.mower_id) not in relay.tiles[0].whep_url
    claims = h.signer.verify(relay.ticket, h.clock())
    assert (claims.account_id, claims.mower_id, claims.lease_id) == (
        relay_key.account_id,
        relay_key.mower_id,
        relay.lease_id,
    )
    assert camera_session.expires_at == claims.expires_at
    assert relay.heartbeat_seconds == 15
    assert [s.camera_ids for s in h.vendor.starts] == [("front_left", "front_right")]
    assert h.events.types() == [CAMERA_RELAY_LEASE_OPENED]
    subject, envelope = h.events.published[0]
    assert parse_camera_relay_event_subject(subject) == (relay_key.mower_id, "lease_opened")
    assert (envelope.account_id, envelope.producer) == (relay_key.account_id, "camera-relay")


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (SourceUnavailableError("no driver"), RelayErrorCode.UNAVAILABLE),
        (UnsupportedProviderError("no driver"), RelayErrorCode.UNSUPPORTED_PROVIDER),
    ],
)
async def test_a_source_that_cannot_start_answers_with_its_error(
    error: Exception, code: RelayErrorCode
) -> None:
    h = Harness()
    assert isinstance(error, SourceUnavailableError | UnsupportedProviderError)
    h.vendor.fail_with = error
    relay_key = key()
    reply = await h.service.attach(attach_request(relay_key))
    assert reply.error is not None and reply.error.code is code
    assert h.events.types() == [
        CAMERA_RELAY_LEASE_OPENED,
        CAMERA_RELAY_LEASE_CLOSED,
        CAMERA_RELAY_STOPPED,
    ]
    assert len(h.vendor.stops) == 1 and h.vendor.stops[0].release
    session = h.repository.get(relay_key)
    assert session is not None and session.state is SessionState.FAILED
    retry = await h.service.attach(attach_request(relay_key))
    assert retry.error is not None and retry.error.code is RelayErrorCode.UNAVAILABLE
    assert len(h.vendor.starts) == 1  # no new start during the backoff


async def test_full_lifecycle_through_the_use_cases() -> None:
    h = Harness()
    relay_key = key()
    camera_session = await _attach(h, relay_key)
    assert camera_session.relay is not None
    lease_id = camera_session.relay.lease_id
    await _go_live(h, relay_key)
    assert await h.service.media_confirmed(lease_id, "whep-1")
    h.clock.advance(10)
    renewed = await h.service.renew(_renew(relay_key, lease_id))
    assert renewed.session is not None and renewed.session.relay is not None
    assert renewed.session.relay.lease_id == lease_id
    detached = await h.service.detach(_detach(relay_key, lease_id))
    assert (detached.detached, detached.error) == (True, None)
    assert h.media.kicked == ["whep-1"]
    h.clock.advance(14)
    await h.service.tick()
    assert h.vendor.stops == []
    h.clock.advance(1)
    await h.service.tick()
    assert len(h.vendor.stops) == 1
    assert h.events.types() == [
        CAMERA_RELAY_LEASE_OPENED,
        CAMERA_RELAY_STARTED,
        CAMERA_RELAY_LEASE_CLOSED,
        CAMERA_RELAY_STOPPED,
    ]
    session = h.repository.get(relay_key)
    assert session is None or session.state is SessionState.IDLE
    assert not await h.service.media_lost(lease_id)


async def test_tick_forgets_dormant_sessions() -> None:
    h = Harness()
    h.vendor.fail_with = SourceUnavailableError("x")
    relay_key = key()
    await h.service.attach(attach_request(relay_key))
    h.clock.advance(5)
    await h.service.tick()
    assert h.repository.get(relay_key) is None


async def test_mower_offline_stops_the_sessions_of_every_account() -> None:
    h = Harness()
    mower_id = uuid4()
    a, b = key(mower_id=mower_id), key(mower_id=mower_id)
    await _attach(h, a)
    await _attach(h, b)
    await h.service.mower_status_changed(mower_id, MowerStatus(online=False))
    assert len(h.vendor.stops) == 2
    reply = await h.service.attach(attach_request(key(mower_id=mower_id)))
    assert reply.error is not None and reply.error.code is RelayErrorCode.UNAVAILABLE


async def test_vision_attach_uses_the_known_mower_state() -> None:
    h = Harness()
    relay_key = key()
    refused = await h.service.attach(attach_request(relay_key, purpose=RelayPurpose.VISION))
    assert refused.error is not None and refused.error.code is RelayErrorCode.UNAVAILABLE
    await h.service.mower_status_changed(relay_key.mower_id, ONLINE)
    other = key(mower_id=relay_key.mower_id)
    admitted = await h.service.attach(attach_request(other, purpose=RelayPurpose.VISION))
    assert admitted.session is not None


async def test_shutdown_stops_running_sessions() -> None:
    h = Harness()
    await _attach(h, key())
    await h.service.shutdown()
    assert h.events.types()[-1] == CAMERA_RELAY_STOPPED


# --- Account isolation --------------------------------------------------------------------------


async def test_same_mower_under_two_accounts_gets_two_sessions() -> None:
    h = Harness()
    mower_id = uuid4()
    a, b = key(mower_id=mower_id), key(mower_id=mower_id)
    session_a = await _attach(h, a)
    session_b = await _attach(h, b)
    assert len(h.vendor.starts) == 2
    assert {s.key for s in h.vendor.starts} == {a, b}
    sa, sb = h.repository.get(a), h.repository.get(b)
    assert sa is not None and sb is not None
    assert sa.relay_id != sb.relay_id and sa.session_id != sb.session_id
    assert session_a.relay is not None and session_b.relay is not None
    urls_a = {t.whep_url for t in session_a.relay.tiles}
    urls_b = {t.whep_url for t in session_b.relay.tiles}
    assert urls_a.isdisjoint(urls_b)
    claims_a = h.signer.verify(session_a.relay.ticket, h.clock())
    claims_b = h.signer.verify(session_b.relay.ticket, h.clock())
    for path in claims_b.paths:
        assert not claims_a.allows(account_id=a.account_id, path=path, now=h.clock())
        assert not claims_a.allows(account_id=b.account_id, path=path, now=h.clock())
    for path in claims_a.paths:
        assert not claims_b.allows(account_id=b.account_id, path=path, now=h.clock())


async def test_another_account_can_neither_renew_nor_detach_a_lease() -> None:
    h = Harness()
    mower_id = uuid4()
    a, b = key(mower_id=mower_id), key(mower_id=mower_id)
    session_a = await _attach(h, a)
    await _attach(h, b)
    assert session_a.relay is not None
    lease_a = session_a.relay.lease_id
    sa = h.repository.get(a)
    assert sa is not None
    expires = sa.leases[lease_a].expires_at

    h.clock.advance(10)
    renew = await h.service.renew(_renew(a, lease_a, account_id=b.account_id))
    assert renew.session is None and renew.error is not None
    assert renew.error.code is RelayErrorCode.FORBIDDEN
    assert sa.leases[lease_a].expires_at == expires  # not extended

    detach = await h.service.detach(_detach(a, lease_a, account_id=b.account_id))
    assert detach.detached is False and detach.error is not None
    assert detach.error.code is RelayErrorCode.FORBIDDEN
    assert lease_a in sa.leases

    sb = h.repository.get(b)
    assert sb is not None and lease_a not in sb.leases
    later = h.events.published[2:]
    assert not any(getattr(e.payload, "lease_id", None) == lease_a for _, e in later)


async def test_a_lease_is_found_only_under_its_own_mower() -> None:
    h = Harness()
    a = key()
    session_a = await _attach(h, a)
    assert session_a.relay is not None
    lease_a = session_a.relay.lease_id
    renew = await h.service.renew(_renew(a, lease_a, mower_id=uuid4()))
    assert renew.error is not None and renew.error.code is RelayErrorCode.NOT_FOUND
    detach = await h.service.detach(_detach(a, lease_a, mower_id=uuid4()))
    assert (detach.detached, detach.error) == (False, None)


async def test_unknown_leases_are_not_found_and_detach_is_a_no_op() -> None:
    h = Harness()
    a = key()
    renew = await h.service.renew(_renew(a, uuid4()))
    assert renew.error is not None and renew.error.code is RelayErrorCode.NOT_FOUND
    detach = await h.service.detach(_detach(a, uuid4()))
    assert (detach.detached, detach.error) == (False, None)
    assert h.events.published == []
    session_a = await _attach(h, a)
    assert session_a.relay is not None
    lease = session_a.relay.lease_id
    assert (await h.service.detach(_detach(a, lease))).detached is True
    again = await h.service.detach(_detach(a, lease))
    assert (again.detached, again.error) == (False, None)


async def test_a_ticket_of_one_account_never_opens_another_accounts_paths() -> None:
    h = Harness()
    a = key()
    session_a = await _attach(h, a)
    assert session_a.relay is not None
    claims = h.signer.verify(session_a.relay.ticket, h.clock())
    assert claims.allows(account_id=a.account_id, path=claims.paths[0], now=at(1))
    assert not claims.allows(account_id=uuid4(), path=claims.paths[0], now=at(1))
