"""Health history over the bus and HTTP: totals, days, battery, blade reset, threshold event."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_authz import RelationshipTuple, account, mower, principal_subject
from mowdeck_contracts.v1 import (
    DRIVE_CORRELATION_PREFIX,
    TELEMETRY_BLADE_HOURS_THRESHOLD,
    BladeHoursThreshold,
    CommandResult,
    CommandStatus,
    EventEnvelope,
    MowerActivity,
    Principal,
    PrincipalKind,
)
from mowdeck_service_kit.bus import publish_command_result
from telemetry_support import API, T0, Harness, Person, harness, make_state, seconds

from telemetry.adapters.bus.health_events import BusHealthEvents
from telemetry.adapters.memory import MemoryStore
from telemetry.application.context import TelemetryConfig
from telemetry.application.services import build_services
from telemetry.domain.errors import AccountMismatchError, NoTelemetryYetError

ISO = "%Y-%m-%dT%H:%M:%SZ"
MOWING = MowerActivity.MOWING
THRESHOLD_SUBJECT = "event.telemetry.blade_hours.threshold"


async def mow(
    h: Harness, owner: Person, mower_id: UUID, start: float, count: int, **extra: Any
) -> None:
    """``count`` mowing states every 10 s from ``T0 + start`` with blades on."""
    for i in range(count):
        await h.publish_state(
            make_state(
                mower_id,
                owner.account_id,
                at=T0 + seconds(start + i * 10),
                activity=MOWING,
                raw={"blade_on": True},
                **extra,
            )
        )


async def subuser(h: Harness, owner: Person, mower_id: UUID) -> Person:
    """A sub-user of the owner's account who may only view ``mower_id``."""
    sub = Principal(subject=uuid4(), account_id=owner.account_id, kind=PrincipalKind.SUBUSER)
    person = Person(sub, h.keys.mint(sub))
    await h.identity.write(
        [RelationshipTuple(principal_subject(sub), "member", account(owner.account_id))]
    )
    await h.identity.replace_subject_grants(principal_subject(sub), mower(mower_id), ["mower.view"])
    return person


async def test_telemetry_health_totals_days_and_battery_over_http() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        account_id = owner.account_id
        for i in range(7):  # 60 s of mowing, 6 steps of about 11 m
            await h.publish_state(
                make_state(
                    mower_id,
                    account_id,
                    at=T0 + seconds(i * 10),
                    activity=MOWING,
                    battery=60 - i,
                    position=(48.0 + i / 10_000, 11.0),
                    raw={"blade_on": i < 3},
                )
            )
        await h.publish_state(
            make_state(
                mower_id,
                account_id,
                at=T0 + seconds(70),
                activity=MowerActivity.ERROR,
                error_code="E42",
                battery=50,
            )
        )
        await h.publish_state(
            make_state(mower_id, account_id, at=T0 + seconds(80), charging=True, battery=50)
        )
        base = f"{API}/mowers/{mower_id}"
        health = await h.client.get(f"{base}/health", headers=owner.headers)
        daily = await h.client.get(
            f"{base}/health/daily",
            headers=owner.headers,
            params={"from": "2026-10-01", "to": "2026-10-31"},
        )
        battery = await h.client.get(
            f"{base}/battery",
            headers=owner.headers,
            params={
                "from": T0.strftime(ISO),
                "to": (T0 + seconds(120)).strftime(ISO),
                "step": "1m",
            },
        )
        raw_battery = await h.client.get(
            f"{base}/battery",
            headers=owner.headers,
            params={"from": T0.strftime(ISO), "to": (T0 + seconds(120)).strftime(ISO)},
        )
    assert health.status_code == 200, health.text
    body = health.json()
    assert body["since"] == "2026-10-09T12:00:00Z"
    assert body["blade_hours"] == round(30 / 3600, 4)  # blade flag true for [0, 30)
    assert body["mowing_hours"] == round(70 / 3600, 4)
    assert body["blade_hours_since_change"] == body["blade_hours"]
    assert body["blade_changed_at"] is None and body["blade_hours_threshold"] == 50.0
    assert body["blade_source"] == "telemetry"  # the simulator has no device counter
    assert body["device_blade_hours"] is None and body["device_blade_warn_hours"] is None
    assert 66 < body["distance_m"] < 67
    assert (body["battery_cycles"], body["charge_sessions"]) == (0, 1)
    assert body["errors"] == {"E42": 1}
    assert body["battery_cycles_source"] == "telemetry"  # nor a battery cycle counter
    assert body["battery_cycles_tracked"] == 0 and body["device_battery_cycles"] is None
    assert daily.status_code == 200, daily.text
    days = daily.json()
    assert (days["from"], days["to"]) == ("2026-10-01", "2026-10-31")
    assert [d["day"] for d in days["days"]] == ["2026-10-09"]
    assert days["days"][0]["errors"] == {"E42": 1}
    assert days["days"][0]["mowing_hours"] == body["mowing_hours"]
    assert battery.status_code == 200, battery.text
    series = battery.json()
    assert series["step_seconds"] == 60 and series["truncated"] is False
    assert [(p["battery_min"], p["battery_max"]) for p in series["points"]] == [(55, 60), (50, 54)]
    assert set(series["points"][0]) == {
        "at",
        "battery_percent",
        "battery_min",
        "battery_max",
        "charging",
    }
    assert series["points"][1]["charging"] is True
    assert raw_battery.json()["step_seconds"] is None
    assert len(raw_battery.json()["points"]) == 9


async def test_telemetry_health_redelivered_and_older_states_count_once() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        first = make_state(mower_id, owner.account_id, at=T0, activity=MOWING)
        second = make_state(mower_id, owner.account_id, at=T0 + seconds(10), activity=MOWING)
        for state in (first, second, second, first):  # redelivery, then an old one
            await h.publish_state(state)
        row = h.store.data.health[mower_id]
        days = list(h.store.data.daily.values())
    assert row.mowing_seconds == 10 and row.blade_seconds == 10
    assert len(days) == 1 and days[0].mowing_seconds == 10


async def test_telemetry_health_threshold_event_once_and_again_after_a_blade_reset() -> None:
    async with harness(telemetry_blade_hours_threshold=0.01) as h:  # 36 s
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await mow(h, owner, mower_id, 0, 10)  # 90 s
        first = h.bus.messages(THRESHOLD_SUBJECT)
        reset = await h.client.post(
            f"{API}/mowers/{mower_id}/health/blade-reset",
            headers=owner.headers,
            json={"note": "new blades"},
        )
        await mow(h, owner, mower_id, 100, 3)  # 30 s since the reset (incl. [90, 100)): below
        between = h.bus.messages(THRESHOLD_SUBJECT)
        await mow(h, owner, mower_id, 130, 3)
        second = h.bus.messages(THRESHOLD_SUBJECT)
    assert len(first) == 1
    event = first[0].model(EventEnvelope[BladeHoursThreshold])
    assert event.type == TELEMETRY_BLADE_HOURS_THRESHOLD and event.producer == "telemetry"
    assert event.account_id == owner.account_id
    payload = event.payload
    assert (payload.mower_id, payload.threshold_hours) == (mower_id, 0.01)
    assert payload.blade_hours_since_change == pytest.approx(40 / 3600)
    assert payload.crossed_at == T0 + seconds(40) and payload.blade_changed_at is None
    assert reset.status_code == 200, reset.text
    assert reset.json()["blade_hours_since_change"] == 0
    assert len(between) == 1 and len(second) == 2
    again = second[1].model(EventEnvelope[BladeHoursThreshold]).payload
    assert again.blade_changed_at is not None and again.crossed_at == T0 + seconds(130)


async def test_telemetry_health_blade_reset_permissions_and_changes() -> None:
    async with harness() as h:
        owner = await h.owner()
        intruder = await h.owner()
        mower_id = await h.add_mower(owner)
        viewer = await subuser(h, owner, mower_id)
        path = f"{API}/mowers/{mower_id}/health/blade-reset"
        before = await h.client.get(f"{API}/mowers/{mower_id}/health", headers=owner.headers)
        ok = await h.client.post(path, headers=owner.headers, json={})
        again = await h.client.post(path, headers=owner.headers, json={"note": "second"})
        forbidden = await h.client.post(path, headers=viewer.headers, json={})
        hidden = await h.client.post(path, headers=intruder.headers, json={})
        too_long = await h.client.post(path, headers=owner.headers, json={"note": "x" * 501})
        after = await h.client.get(f"{API}/mowers/{mower_id}/health", headers=owner.headers)
        viewed = await h.client.get(f"{API}/mowers/{mower_id}/health", headers=viewer.headers)
        changes = await h.client.get(
            f"{API}/mowers/{mower_id}/health/blade-changes", headers=viewer.headers
        )
    assert before.status_code == 404 and before.json()["detail"] == "no telemetry yet"
    assert ok.status_code == 200, ok.text
    assert ok.json()["since"] is None and ok.json()["blade_changed_at"] is not None
    assert again.status_code == 200
    assert forbidden.status_code == 403 and hidden.status_code == 404
    assert too_long.status_code == 422
    assert after.status_code == 200 and after.json()["blade_hours"] == 0
    assert viewed.status_code == 200
    items = changes.json()["items"]
    assert [i["note"] for i in items] == ["second", None]  # newest first
    assert items[0]["changed_by"] == str(owner.principal.subject)


async def test_telemetry_health_routes_hide_foreign_mowers() -> None:
    async with harness() as h:
        owner = await h.owner()
        intruder = await h.owner()
        mower_id = await h.add_mower(owner)
        await mow(h, owner, mower_id, 0, 3)
        base = f"{API}/mowers/{mower_id}"
        for suffix in ("health", "health/daily", "battery", "health/blade-changes"):
            own = await h.client.get(f"{base}/{suffix}", headers=owner.headers)
            foreign = await h.client.get(f"{base}/{suffix}", headers=intruder.headers)
            smuggled = await h.client.get(
                f"{base}/{suffix}",
                headers=intruder.headers,
                params={"account_id": str(owner.account_id)},
            )
            assert own.status_code == 200, (suffix, own.text)
            assert foreign.status_code == 404 and smuggled.status_code == 404, suffix
        unknown = await h.client.get(f"{API}/mowers/{uuid4()}/health", headers=owner.headers)
    assert unknown.status_code == 404


async def test_telemetry_health_daily_defaults_to_the_last_30_utc_days() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await mow(h, owner, mower_id, 0, 2)
        h.app.state.telemetry.health._clock = lambda: T0 + timedelta(days=3)
        response = await h.client.get(
            f"{API}/mowers/{mower_id}/health/daily", headers=owner.headers
        )
        h.app.state.telemetry.health._clock = lambda: T0 + timedelta(days=40)
        later = await h.client.get(f"{API}/mowers/{mower_id}/health/daily", headers=owner.headers)
    body = response.json()
    assert (body["from"], body["to"]) == ("2026-09-13", "2026-10-12")
    assert [d["day"] for d in body["days"]] == ["2026-10-09"]
    assert later.json()["days"] == []


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"from": "2026-10-09", "to": "2026-10-01"}, "after"),
        ({"from": "2025-01-01", "to": "2026-10-01"}, "at most 366 days"),
        ({"from": "yesterday"}, ""),
    ],
)
async def test_telemetry_health_daily_validates_the_range(
    params: dict[str, str], fragment: str
) -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        response = await h.client.get(
            f"{API}/mowers/{mower_id}/health/daily", headers=owner.headers, params=params
        )
    assert response.status_code == 422, response.text
    assert fragment in str(response.json())


async def test_telemetry_health_daily_accepts_exactly_366_days() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        response = await h.client.get(
            f"{API}/mowers/{mower_id}/health/daily",
            headers=owner.headers,
            params={"from": "2025-10-09", "to": "2026-10-09"},
        )
    assert response.status_code == 200, response.text


async def test_telemetry_health_drive_frames_accepted_results_are_not_stored() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        drive = f"{DRIVE_CORRELATION_PREFIX}{uuid4()}"
        for status in (CommandStatus.ACCEPTED, CommandStatus.REJECTED):
            await publish_command_result(
                h.bus,
                CommandResult(
                    command_id=uuid4(),
                    mower_id=mower_id,
                    status=status,
                    at=T0,
                    correlation_id=drive,
                ),
            )
        for correlation in ("drive:not-a-session", "req-1"):  # not drive sessions: stored
            await publish_command_result(
                h.bus,
                CommandResult(
                    command_id=uuid4(),
                    mower_id=mower_id,
                    status=CommandStatus.ACCEPTED,
                    at=T0,
                    correlation_id=correlation,
                ),
            )
        stored = sorted(e.status for e in h.store.data.commands.values())
    assert stored == ["accepted", "accepted", "rejected"]


class _FailingEvents:
    def __init__(self) -> None:
        self.calls = 0

    async def blade_threshold(self, event: BladeHoursThreshold) -> None:
        self.calls += 1
        raise RuntimeError("bus down")


async def test_telemetry_health_publish_failure_is_logged_not_raised() -> None:
    store = MemoryStore()
    events = _FailingEvents()
    config = TelemetryConfig(blade_hours_threshold=10 / 3600)
    services = build_services(store, config, lambda: T0, events)
    mower_id, account_id = uuid4(), uuid4()
    for i in range(3):
        stored = await services.ingest.record_state(
            make_state(mower_id, account_id, at=T0 + seconds(i * 10), activity=MOWING)
        )
        assert stored is True
    assert events.calls == 1
    assert store.data.health[mower_id].blade_threshold_notified is True


async def test_telemetry_health_rows_keep_their_account() -> None:
    store = MemoryStore()
    config = TelemetryConfig(blade_hours_threshold=5 / 3600)
    services = build_services(store, config, lambda: T0)  # no publisher: crossings stay local
    mower_id, account_id, other = uuid4(), uuid4(), uuid4()
    await services.health.reset_blade(account_id, mower_id, None, None)
    with pytest.raises(AccountMismatchError):
        await services.ingest.record_state(make_state(mower_id, other))
    with pytest.raises(NoTelemetryYetError):
        await services.health.reset_blade(other, mower_id, None, "foreign")
    with pytest.raises(NoTelemetryYetError):
        await services.health.totals(other, mower_id)
    assert len(store.data.blade_changes) == 1
    await services.ingest.record_state(make_state(mower_id, account_id, activity=MOWING))
    await services.ingest.record_state(
        make_state(mower_id, account_id, at=T0 + seconds(10), activity=MOWING)
    )
    row = store.data.health[mower_id]
    assert row.since == T0 and row.blade_threshold_notified is True


async def test_telemetry_health_prefers_the_device_blade_counter() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await mow(h, owner, mower_id, 0, 2)
        await h.publish_state(
            make_state(
                mower_id,
                owner.account_id,
                at=T0 + seconds(30),
                raw={"blade_used_s": 12 * 3600, "blade_used_warn_s": 100 * 3600},
            )
        )
        response = await h.client.get(f"{API}/mowers/{mower_id}/health", headers=owner.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["blade_source"] == "device"
    assert (body["device_blade_hours"], body["device_blade_warn_hours"]) == (12.0, 100.0)
    assert body["blade_hours_since_change"] == round(30 / 3600, 4)  # own counter stays


async def test_telemetry_health_prefers_the_device_battery_cycle_counter() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.publish_state(make_state(mower_id, owner.account_id, at=T0, battery=10))
        await h.publish_state(
            make_state(mower_id, owner.account_id, at=T0 + seconds(10), battery=90)
        )
        response = await h.client.get(f"{API}/mowers/{mower_id}/health", headers=owner.headers)
        own = response.json()
        await h.publish_state(
            make_state(
                mower_id,
                owner.account_id,
                at=T0 + seconds(20),
                battery=90,
                raw={"bat_cycles": 152},
            )
        )
        response = await h.client.get(f"{API}/mowers/{mower_id}/health", headers=owner.headers)
    assert response.status_code == 200, response.text
    assert own["battery_cycles_source"] == "telemetry"
    assert own["battery_cycles"] == own["battery_cycles_tracked"] == 1
    assert own["device_battery_cycles"] is None
    body = response.json()
    assert body["battery_cycles_source"] == "device"
    assert (body["battery_cycles"], body["device_battery_cycles"]) == (152, 152)
    assert body["battery_cycles_tracked"] == 1  # MowDeck's own count stays available


async def test_telemetry_health_events_without_a_bus_are_a_no_op() -> None:
    payload = BladeHoursThreshold(
        mower_id=uuid4(),
        account_id=uuid4(),
        blade_hours_since_change=51,
        threshold_hours=50,
        crossed_at=T0,
    )
    await BusHealthEvents(lambda: None).blade_threshold(payload)


async def test_telemetry_report_summary_lists_day_totals_of_visible_mowers() -> None:
    async with harness() as h:
        owner = await h.owner()
        first = await h.add_mower(owner)
        second = await h.add_mower(owner)
        idle = await h.add_mower(owner)  # no data in the range: absent
        await mow(h, owner, first, 0, 7)  # one job, 60 s of blades and online time
        await mow(h, owner, second, 0, 4)
        h.app.state.telemetry.health._clock = lambda: T0 + timedelta(hours=1)
        response = await h.client.get(f"{API}/reports/summary", headers=owner.headers)
        narrowed = await h.client.get(
            f"{API}/reports/summary",
            headers=owner.headers,
            params={"mower_id": [str(second), str(idle)]},
        )
        sub = await subuser(h, owner, second)
        limited = await h.client.get(f"{API}/reports/summary", headers=sub.headers)
        smuggled = await h.client.get(
            f"{API}/reports/summary", headers=sub.headers, params={"mower_id": str(first)}
        )
        intruder = await h.owner()
        foreign = await h.client.get(
            f"{API}/reports/summary",
            headers=intruder.headers,
            params={"account_id": str(owner.account_id)},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["from"], body["to"], body["truncated"]) == ("2026-09-10", "2026-10-09", False)
    by_mower = {item["mower_id"]: item["days"] for item in body["items"]}
    assert set(by_mower) == {str(first), str(second)}
    day = by_mower[str(first)][0]
    assert day["day"] == "2026-10-09" and day["jobs"] == 1
    assert day["blade_hours"] == pytest.approx(60 / 3600, abs=1e-4)
    assert day["online_hours"] == pytest.approx(60 / 3600, abs=1e-4)
    assert [item["mower_id"] for item in narrowed.json()["items"]] == [str(second)]
    assert [item["mower_id"] for item in limited.json()["items"]] == [str(second)]
    assert smuggled.json()["items"] == []
    assert foreign.status_code == 200 and foreign.json()["items"] == []


async def test_telemetry_report_summary_validates_the_range() -> None:
    async with harness() as h:
        owner = await h.owner()
        reversed_range = await h.client.get(
            f"{API}/reports/summary",
            headers=owner.headers,
            params={"from": "2026-10-09", "to": "2026-10-01"},
        )
        too_long = await h.client.get(
            f"{API}/reports/summary",
            headers=owner.headers,
            params={"from": "2025-01-01", "to": "2026-10-01"},
        )
        anonymous = await h.client.get(f"{API}/reports/summary")
    assert reversed_range.status_code == 422 and "after" in reversed_range.text
    assert too_long.status_code == 422 and "366" in too_long.text
    assert anonymous.status_code == 401


async def test_telemetry_report_summary_flags_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telemetry.application import health as health_module

    monkeypatch.setattr(health_module, "REPORT_MAX_ROWS", 1)
    async with harness() as h:
        owner = await h.owner()
        for _ in range(2):
            await mow(h, owner, await h.add_mower(owner), 0, 2)
        h.app.state.telemetry.health._clock = lambda: T0 + timedelta(hours=1)
        body = (await h.client.get(f"{API}/reports/summary", headers=owner.headers)).json()
    assert body["truncated"] is True and len(body["items"]) == 1
