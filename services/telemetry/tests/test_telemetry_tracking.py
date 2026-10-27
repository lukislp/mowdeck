"""Theft tracking mode (ADR 0053): state machine, lease, events, share links, public view,
report, permissions, retention."""

import base64
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

from mowdeck_authz import RelationshipTuple, account, mower, principal_subject
from mowdeck_contracts.v1 import (
    NOTIFICATION_RAISED,
    TRACKING_ENDED,
    TRACKING_MOVED,
    TRACKING_STALE,
    TRACKING_STARTED,
    EventEnvelope,
    MowerActivity,
    NotificationKind,
    NotificationRaised,
    NotificationSeverity,
    PhotoReply,
    PhotoRequest,
    Principal,
    PrincipalKind,
    TrackingEvent,
    TrackingLeaseCode,
    TrackingLeaseReply,
    TrackingLeaseRequest,
)
from mowdeck_service_kit.bus import BusMessage, publish_event
from PIL import Image
from telemetry_support import API, T0, Harness, Person, harness, make_state, seconds

from telemetry.application.tracking import Tracking
from telemetry.domain.tracking import is_plausible_token, new_token, token_hash


class Clock:
    """A clock the test moves."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class Gateway:
    """Records the leases and answers photos."""

    leases: list[TrackingLeaseRequest] = field(default_factory=list)
    photo_requests: list[PhotoRequest] = field(default_factory=list)
    photo_bytes: bytes | None = None
    reply: TrackingLeaseReply = field(
        default_factory=lambda: TrackingLeaseReply(code=TrackingLeaseCode.OK, interval_seconds=10)
    )

    async def lease(self, request: TrackingLeaseRequest) -> TrackingLeaseReply | None:
        self.leases.append(request)
        return self.reply

    async def photo(self, request: PhotoRequest) -> PhotoReply | None:
        self.photo_requests.append(request)
        if self.photo_bytes is None:
            return None
        return PhotoReply(
            image_base64=base64.b64encode(self.photo_bytes).decode(), media_type="image/jpeg"
        )


def jpeg() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 48), (30, 120, 60)).save(buffer, "JPEG")
    return buffer.getvalue()


async def collect(h: Harness) -> list[tuple[str, TrackingEvent]]:
    """Subscribe to the tracking events; the list fills as they are published."""
    events: list[tuple[str, TrackingEvent]] = []

    async def handle(message: BusMessage) -> None:
        envelope = message.model(EventEnvelope[dict[str, Any]])
        events.append((envelope.type, TrackingEvent.model_validate(envelope.payload)))

    await h.bus.subscribe("event.tracking.>", handle)
    return events


def tracking_of(h: Harness) -> Tracking:
    tracking: Tracking | None = h.app.state.telemetry.tracking
    assert tracking is not None
    return tracking


async def setup(h: Harness, *, with_position: bool = True) -> tuple[Person, UUID]:
    owner = await h.owner()
    mower_id = await h.add_mower(owner)
    if with_position:
        await h.publish_state(make_state(mower_id, owner.account_id, at=T0, position=(48.0, 11.0)))
    return owner, mower_id


async def viewer(h: Harness, owner: Person, mower_id: UUID) -> Person:
    sub = Principal(subject=uuid4(), account_id=owner.account_id, kind=PrincipalKind.SUBUSER)
    person = Person(sub, h.keys.mint(sub))
    await h.identity.write(
        [RelationshipTuple(principal_subject(sub), "member", account(owner.account_id))]
    )
    await h.identity.replace_subject_grants(principal_subject(sub), mower(mower_id), ["mower.view"])
    return person


async def public(h: Harness, token: str) -> Any:
    return await h.client.post(f"{API}/public/tracking/view", json={"token": token})


# --- state machine -------------------------------------------------------------------------


async def test_tracking_manual_start_is_idempotent_leases_and_ends_as_false_alarm() -> None:
    gateway = Gateway()
    clock = Clock()
    async with harness(clock=clock, tracking_gateway=gateway) as h:
        events = await collect(h)
        owner, mower_id = await setup(h)
        url = f"{API}/mowers/{mower_id}/tracking"
        first = await h.client.post(url, headers=owner.headers)
        again = await h.client.post(url, headers=owner.headers)
        assert (first.status_code, again.status_code) == (201, 200)
        assert first.json()["id"] == again.json()["id"]
        assert first.json()["trigger"] == "manual"
        assert first.json()["interval_seconds"] == 10
        assert first.json()["last"]["lat"] == 48.0
        assert gateway.leases[0].interval_seconds == 10
        assert gateway.leases[0].lease_seconds == 60
        stopped = await h.client.post(
            f"{url}/stop", headers=owner.headers, json={"reason": "false_alarm"}
        )
        assert stopped.json()["active"] is False
        assert stopped.json()["end_reason"] == "false_alarm"
        assert gateway.leases[-1].lease_seconds == 0  # released at once
        assert [t for t, _ in events] == [TRACKING_STARTED, TRACKING_ENDED]
        assert events[1][1].end_reason == "false_alarm"
        nothing = await h.client.post(f"{url}/stop", headers=owner.headers, json={})
        assert nothing.status_code == 404


async def test_tracking_starts_on_theft_alarm_only() -> None:
    gateway = Gateway()
    async with harness(clock=Clock(), tracking_gateway=gateway) as h:
        owner, mower_id = await setup(h)

        async def raise_(kind: NotificationKind) -> None:
            raised = NotificationRaised(
                notification_id=uuid4(),
                account_id=owner.account_id,
                mower_id=mower_id,
                kind=kind,
                severity=NotificationSeverity.CRITICAL,
                title="t",
                body="b",
                occurred_at=T0,
                created_at=T0,
                dedupe_key=str(uuid4()),
            )
            await publish_event(
                h.bus,
                EventEnvelope[NotificationRaised](
                    type=NOTIFICATION_RAISED,
                    producer="notifications",
                    account_id=owner.account_id,
                    payload=raised,
                ),
            )

        await raise_(NotificationKind.BATTERY_LOW)
        status = await h.client.get(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        assert status.json()["session"] is None
        await raise_(NotificationKind.MOWER_LIFTED)
        await raise_(NotificationKind.GEOFENCE_EXIT)  # already tracked: no second session
        status = await h.client.get(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        assert status.json()["session"]["trigger"] == "mower_lifted"
        listed = await h.client.get(
            f"{API}/mowers/{mower_id}/tracking/sessions", headers=owner.headers
        )
        assert len(listed.json()) == 1


async def test_tracking_alarm_start_can_be_switched_off() -> None:
    async with harness(
        clock=Clock(), tracking_gateway=Gateway(), telemetry_tracking_auto_start=False
    ) as h:
        owner, mower_id = await setup(h)
        await publish_event(
            h.bus,
            EventEnvelope[NotificationRaised](
                type=NOTIFICATION_RAISED,
                producer="notifications",
                account_id=owner.account_id,
                payload=NotificationRaised(
                    notification_id=uuid4(),
                    account_id=owner.account_id,
                    mower_id=mower_id,
                    kind=NotificationKind.MOWER_LIFTED,
                    severity=NotificationSeverity.CRITICAL,
                    title="t",
                    body="b",
                    occurred_at=T0,
                    created_at=T0,
                    dedupe_key="x",
                ),
            ),
        )
        status = await h.client.get(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        assert status.json()["session"] is None


async def test_tracking_ends_after_the_configured_maximum_and_interval_is_configurable() -> None:
    clock = Clock()
    gateway = Gateway()
    async with harness(
        clock=clock,
        tracking_gateway=gateway,
        telemetry_tracking_max_hours=2,
        telemetry_tracking_interval_seconds=20,
    ) as h:
        events = await collect(h)
        owner, mower_id = await setup(h)
        started = await h.client.post(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        assert started.json()["interval_seconds"] == 20
        tracking = tracking_of(h)
        clock.advance(hours=1, minutes=59)
        await h.publish_state(
            make_state(mower_id, owner.account_id, at=clock.now, position=(48.0, 11.0))
        )
        await tracking.tick()
        assert (
            await h.client.get(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        ).json()["session"]["active"]
        assert gateway.leases[-1].interval_seconds == 20
        clock.advance(minutes=2)
        await tracking.tick()
        ended = (
            await h.client.get(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        ).json()
        assert ended["session"]["end_reason"] == "max_duration"
        assert events[-1][0] == TRACKING_ENDED
        assert events[-1][1].end_reason == "max_duration"
        count = len(gateway.leases)
        await tracking.tick()
        assert len(gateway.leases) == count  # nothing renewed for an ended session


async def test_tracking_only_one_replica_ticks() -> None:
    async with harness(clock=Clock(), tracking_gateway=Gateway()) as h:
        owner, mower_id = await setup(h)
        await h.client.post(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        store = tracking_of(h)._store
        store.leader_elsewhere = True  # type: ignore[attr-defined]
        assert await tracking_of(h).tick() == 0


# --- offline / stale / moved ---------------------------------------------------------------


async def test_tracking_stale_once_per_outage_keeps_last_position_and_recovers() -> None:
    clock = Clock()
    async with harness(clock=clock, tracking_gateway=Gateway()) as h:
        events = await collect(h)
        owner, mower_id = await setup(h)
        await h.client.post(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        tracking = tracking_of(h)
        clock.advance(minutes=5)
        await tracking.tick()
        await tracking.tick()
        stale = [e for t, e in events if t == TRACKING_STALE]
        assert len(stale) == 1 and stale[0].stale_minutes == 5
        assert stale[0].lat == 48.0
        view = (
            await h.client.get(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        ).json()
        assert view["session"]["stale_since"] is not None
        assert view["session"]["last"]["lat"] == 48.0  # the last known position stays
        await h.publish_state(
            make_state(mower_id, owner.account_id, at=clock.now, position=(48.0001, 11.0))
        )
        await tracking.tick()
        view = (
            await h.client.get(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        ).json()
        assert view["session"]["stale_since"] is None
        clock.advance(minutes=10)  # silent again: a new outage notifies again
        await tracking.tick()
        assert len([1 for t, _ in events if t == TRACKING_STALE]) == 2


async def test_tracking_moved_needs_distance_and_five_minutes() -> None:
    clock = Clock()
    async with harness(clock=clock, tracking_gateway=Gateway()) as h:
        events = await collect(h)
        owner, mower_id = await setup(h)
        await h.client.post(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        tracking = tracking_of(h)

        async def report(lat: float, minutes: float) -> None:
            clock.advance(minutes=minutes)
            await h.publish_state(
                make_state(mower_id, owner.account_id, at=clock.now, position=(lat, 11.0))
            )
            await tracking.tick()

        def moved() -> list[TrackingEvent]:
            return [e for t, e in events if t == TRACKING_MOVED]

        await report(48.0002, 1)  # 22 m, below 50 m
        assert moved() == []
        await report(48.001, 1)  # 111 m, but only two minutes since the start
        assert moved() == []
        await report(48.0011, 4)  # five minutes passed; 122 m from the reference
        assert len(moved()) == 1
        assert 100 < (moved()[0].distance_m or 0) < 130
        await report(48.0030, 1)  # far, but the notice interval is not over
        assert len(moved()) == 1
        await report(48.0031, 5)
        assert len(moved()) == 2


async def test_tracking_gateway_without_answer_does_not_break_the_tick() -> None:
    class Silent(Gateway):
        async def lease(self, request: TrackingLeaseRequest) -> TrackingLeaseReply | None:
            raise TimeoutError

    async with harness(clock=Clock(), tracking_gateway=Silent()) as h:
        owner, mower_id = await setup(h)
        started = await h.client.post(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        assert started.status_code == 201
        assert await tracking_of(h).tick() == 1


# --- permissions ---------------------------------------------------------------------------


async def test_tracking_permissions_viewer_reads_but_cannot_control_and_foreign_is_404() -> None:
    async with harness(clock=Clock(), tracking_gateway=Gateway()) as h:
        owner, mower_id = await setup(h)
        await h.client.post(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        see = await viewer(h, owner, mower_id)
        base = f"{API}/mowers/{mower_id}/tracking"
        assert (await h.client.get(base, headers=see.headers)).status_code == 200
        assert (await h.client.get(f"{base}/report.pdf", headers=see.headers)).status_code == 200
        assert (await h.client.get(f"{base}/links", headers=see.headers)).status_code == 200
        assert (
            await h.client.post(f"{base}/stop", headers=see.headers, json={})
        ).status_code == 403
        assert (
            await h.client.post(f"{base}/links", headers=see.headers, json={})
        ).status_code == 403
        assert (await h.client.post(base, headers=see.headers)).status_code == 403
        stranger = await h.owner()
        for method, path in (("GET", base), ("POST", base), ("GET", f"{base}/report.pdf")):
            r = await h.client.request(method, path, headers=stranger.headers)
            assert r.status_code == 404
        assert (await h.client.get(base)).status_code == 401


# --- share links ---------------------------------------------------------------------------


def test_token_helpers() -> None:
    token = new_token()
    assert is_plausible_token(token) and len(token) >= 43
    assert token_hash(token) != token and len(token_hash(token)) == 64
    assert not is_plausible_token("short") and not is_plausible_token("a b" * 20)


async def test_tracking_link_shows_only_the_allowed_fields_and_stores_only_the_hash() -> None:
    clock = Clock()
    async with harness(clock=clock, tracking_gateway=Gateway()) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.announce(mower_id, owner.account_id, options={"device_name": "Luba-ABC123"})
        await h.publish_state(make_state(mower_id, owner.account_id, at=T0, position=(48.0, 11.0)))
        base = f"{API}/mowers/{mower_id}/tracking"
        await h.client.post(base, headers=owner.headers)
        for i in range(3):
            await h.publish_state(
                make_state(
                    mower_id,
                    owner.account_id,
                    at=T0 + seconds(10 * (i + 1)),
                    position=(48.0 + i / 10_000, 11.0),
                    raw={"secret": "x"},
                )
            )
        clock.advance(minutes=2)
        created = await h.client.post(f"{base}/links", headers=owner.headers, json={})
        assert created.status_code == 201
        token = created.json()["token"]
        link = created.json()["link"]
        store = tracking_of(h)._store
        stored = store.links  # type: ignore[attr-defined]
        assert token not in repr(stored) and token_hash(token) in repr(stored)
        expiry = datetime.fromisoformat(link["expires_at"])
        assert expiry - clock.now == timedelta(hours=48)

        response = await public(h, token)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "mower_name",
            "mower_model",
            "active",
            "started_at",
            "ended_at",
            "last",
            "stale_since",
            "track",
            "expires_at",
            "server_time",
        }
        assert body["mower_name"] == "Front lawn" and body["active"] is True
        assert body["last"]["lat"] > 48.0 and len(body["track"]) >= 3
        assert set(body["track"][0]) == {"lat", "lon", "at"}
        text = response.text
        assert str(owner.account_id) not in text and str(mower_id) not in text
        assert "Luba-ABC123" not in text  # the serial is for the owner's report only
        assert response.headers["x-robots-tag"].startswith("noindex")
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in response.headers["content-security-policy"]

        listing = await h.client.get(f"{base}/links", headers=owner.headers)
        assert token not in listing.text and "token_hash" not in listing.text
        assert listing.json()[0]["uses"] == 1


async def test_tracking_link_expiry_revoke_ttl_cap_and_limit() -> None:
    clock = Clock()
    async with harness(
        clock=clock, tracking_gateway=Gateway(), telemetry_tracking_max_links=3
    ) as h:
        owner, mower_id = await setup(h)
        base = f"{API}/mowers/{mower_id}/tracking"
        assert (
            await h.client.post(f"{base}/links", headers=owner.headers, json={})
        ).status_code == 404
        await h.client.post(base, headers=owner.headers)
        short = (
            await h.client.post(f"{base}/links", headers=owner.headers, json={"hours": 1})
        ).json()
        assert (await public(h, short["token"])).status_code == 200
        clock.advance(hours=1, seconds=1)
        assert (await public(h, short["token"])).status_code == 404  # expired
        too_long = await h.client.post(f"{base}/links", headers=owner.headers, json={"hours": 100})
        assert too_long.status_code == 422
        revoked = (await h.client.post(f"{base}/links", headers=owner.headers, json={})).json()
        assert (await public(h, revoked["token"])).status_code == 200
        deleted = await h.client.delete(
            f"{base}/links/{revoked['link']['id']}", headers=owner.headers
        )
        assert deleted.status_code == 204
        assert (await public(h, revoked["token"])).status_code == 404  # revoked at once
        again = await h.client.delete(
            f"{base}/links/{revoked['link']['id']}", headers=owner.headers
        )
        assert again.status_code == 404
        for _ in range(3):
            ok = await h.client.post(f"{base}/links", headers=owner.headers, json={})
            assert ok.status_code == 201
        full = await h.client.post(f"{base}/links", headers=owner.headers, json={})
        assert full.status_code == 409  # expired and revoked links do not count, active do


async def test_tracking_link_survives_the_end_of_the_session_until_it_expires() -> None:
    async with harness(clock=Clock(), tracking_gateway=Gateway()) as h:
        owner, mower_id = await setup(h)
        base = f"{API}/mowers/{mower_id}/tracking"
        await h.client.post(base, headers=owner.headers)
        token = (await h.client.post(f"{base}/links", headers=owner.headers, json={})).json()[
            "token"
        ]
        await h.client.post(f"{base}/stop", headers=owner.headers, json={})
        body = (await public(h, token)).json()
        assert body["active"] is False and body["ended_at"] is not None


async def test_public_view_unknown_tokens_look_alike_and_are_rate_limited() -> None:
    async with harness(
        clock=Clock(), tracking_gateway=Gateway(), telemetry_tracking_public_failures_per_minute=3
    ) as h:
        shapes = [new_token(), "x", "A" * 43, "../../etc/passwd"]
        codes = {(await public(h, t)).status_code for t in shapes[:3]}
        assert codes == {404}
        blocked = await public(h, new_token())
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1
        assert blocked.headers["x-robots-tag"].startswith("noindex")
        miss = await h.client.post(f"{API}/public/tracking/view", json={})
        assert miss.status_code == 422


# --- report --------------------------------------------------------------------------------


async def test_tracking_report_pdf_with_photo_serial_and_blank_owner_fields() -> None:
    clock = Clock()
    gateway = Gateway(photo_bytes=jpeg())
    async with harness(clock=clock, tracking_gateway=gateway) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.announce(mower_id, owner.account_id, options={"device_name": "Luba-ABC123"})
        await h.publish_state(make_state(mower_id, owner.account_id, at=T0, position=(48.0, 11.0)))
        base = f"{API}/mowers/{mower_id}/tracking"
        await h.client.post(base, headers=owner.headers)
        for i in range(60):
            await h.publish_state(
                make_state(
                    mower_id,
                    owner.account_id,
                    at=T0 + seconds(10 * (i + 1)),
                    position=(48.0 + i / 5000, 11.0 + i / 9000),
                )
            )
        clock.advance(hours=1)
        photo = await h.client.post(f"{base}/photo", headers=owner.headers)
        assert photo.status_code == 201 and photo.json()["photos"] >= 1
        assert gateway.photo_requests[0].demand.value == "error_analysis"
        for lang in ("en", "de"):
            pdf = await h.client.get(
                f"{base}/report.pdf", params={"lang": lang}, headers=owner.headers
            )
            assert pdf.status_code == 200
            assert pdf.headers["content-type"] == "application/pdf"
            assert pdf.content.startswith(b"%PDF-") and pdf.content.rstrip().endswith(b"%%EOF")
            assert len(pdf.content) > 4000
        # The text of the PDF is compressed; the data the report is built from is checked on
        # the model, the PDF only has to be a valid, non-trivial document.
        view = (await h.client.get(base, headers=owner.headers)).json()
        assert view["session"]["mower_serial"] == "Luba-ABC123"
        assert view["session"]["mower_name"] == "Front lawn"
        assert len(view["track"]) == 60


def test_report_contents_and_empty_track() -> None:

    from telemetry.application.report import PhotoData, ReportData, build_report

    data = ReportData(
        mower_name="Rasen ☃ Mäher",
        mower_model="LUBA 2 AWD X",
        mower_serial=None,
        trigger="mower_lifted",
        started_at=T0,
        ended_at=None,
        generated_at=T0,
        points=[],
        photos=[PhotoData(T0, jpeg())],
    )
    assert build_report(data, "de").startswith(b"%PDF-")  # no track, non-Latin-1 name
    track = [(48.0, 11.0, T0), (48.001, 11.001, T0 + seconds(10))]
    pdf = build_report(replace(data, points=track), "en")
    assert pdf.startswith(b"%PDF-")


# --- session management and retention --------------------------------------------------------


async def test_tracking_delete_and_retention() -> None:
    clock = Clock()
    async with harness(clock=clock, tracking_gateway=Gateway()) as h:
        owner, mower_id = await setup(h)
        base = f"{API}/mowers/{mower_id}/tracking"
        session = (await h.client.post(base, headers=owner.headers)).json()
        running = await h.client.delete(f"{base}/sessions/{session['id']}", headers=owner.headers)
        assert running.status_code == 409  # end it first
        await h.client.post(f"{base}/stop", headers=owner.headers, json={})
        assert (
            await h.client.delete(f"{base}/sessions/{session['id']}", headers=owner.headers)
        ).status_code == 204
        assert (await h.client.get(base, headers=owner.headers)).json()["session"] is None
        second = (await h.client.post(base, headers=owner.headers)).json()
        await h.client.post(f"{base}/stop", headers=owner.headers, json={})
        tracking = tracking_of(h)
        clock.advance(days=29)
        assert await tracking.retain() == 0
        clock.advance(days=2)
        assert await tracking.retain() == 1
        assert (await h.client.get(f"{base}/sessions", headers=owner.headers)).json() == []
        del second


async def test_tracking_unknown_mower_cannot_start() -> None:
    async with harness(clock=Clock(), tracking_gateway=Gateway()) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, announce=False)  # never announced: not in the directory
        r = await h.client.post(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        assert r.status_code == 404


async def test_standing_mower_activity_is_not_needed_for_a_track() -> None:
    clock = Clock()
    async with harness(clock=clock, tracking_gateway=Gateway()) as h:
        owner, mower_id = await setup(h)
        base = f"{API}/mowers/{mower_id}/tracking"
        await h.client.post(base, headers=owner.headers)
        for i in range(5):
            await h.publish_state(
                make_state(
                    mower_id,
                    owner.account_id,
                    at=T0 + seconds(10 * (i + 1)),
                    activity=MowerActivity.ERROR,
                    position=(48.0 + i / 100_000, 11.0),
                )
            )
        clock.advance(minutes=5)
        full = (await h.client.get(base, headers=owner.headers)).json()
        newer = (
            await h.client.get(
                base, params={"after": full["track"][-2]["at"]}, headers=owner.headers
            )
        ).json()
        assert len(newer["track"]) == 1
