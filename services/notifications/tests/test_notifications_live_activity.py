"""Live Activities and widget reload pushes (ADR 0059)."""

from typing import Any
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import GeoReference, JobProgress, MapArea, MowerActivity, MowerMapSnapshot
from mowdeck_contracts.v1 import MowerMap as ContractMap
from notifications_support import API, T0, harness, make_state

from notifications.domain.models import DeliveryOutcome, DeliveryResult

START_TOKEN = "a1" * 32
UPDATE_TOKEN = "b2" * 32
DEVICE_TOKEN = "c3" * 32
ME = f"{API}/me/live-activities"


class ApnsRecorder:
    """Stands in for the APNs channel: records ``push`` calls and answers as scripted."""

    name = "apns"

    def __init__(self) -> None:
        self.pushes: list[dict[str, Any]] = []
        self.results: list[DeliveryResult] = []

    async def send(self, delivery: Any) -> DeliveryResult:
        return DeliveryResult(DeliveryOutcome.SENT)

    async def push(
        self,
        token: str,
        payload: dict[str, Any],
        *,
        push_type: str,
        priority: int,
        expiration: int = 0,
    ) -> DeliveryResult:
        self.pushes.append(
            {
                "token": token,
                "payload": payload,
                "push_type": push_type,
                "priority": priority,
                "event": payload["aps"].get("event"),
            }
        )
        return self.results.pop(0) if self.results else DeliveryResult(DeliveryOutcome.SENT)


def mowing(mower_id: UUID, account_id: UUID, progress: int, **extra: Any) -> Any:
    return make_state(
        mower_id,
        account_id,
        activity=MowerActivity.MOWING,
        job=JobProgress(progress_percent=progress, area_id="front", remaining_s=1800),
        **extra,
    )


async def register(h: Any, owner: Any, installation: UUID, **body: Any) -> Any:
    payload = {"installation_id": str(installation), "push_to_start_token": START_TOKEN, **body}
    response = await h.client.put(f"{ME}/device", json=payload, headers=owner.headers)
    assert response.status_code == 200, response.text
    return response


async def test_a_job_starts_updates_and_ends_the_live_activity() -> None:
    recorder = ApnsRecorder()
    async with harness(channels={"apns": recorder}) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Luba")
        installation = uuid4()
        registered = await register(h, owner, installation)
        watcher = h.services.watcher
        await watcher.on_map(
            MowerMapSnapshot(
                mower_id=mower_id,
                account_id=owner.account_id,
                observed_at=T0,
                map=ContractMap(
                    reference=GeoReference(origin_lat=48.1, origin_lon=11.5),
                    areas=[
                        MapArea(
                            id="front",
                            name="Großer Garten",
                            boundary=[(0, 0), (9, 0), (9, 9), (0, 9)],
                        )
                    ],
                ),
            )
        )
        await watcher.on_state(mowing(mower_id, owner.account_id, 5))
        started = list(recorder.pushes)
        report = await h.client.put(
            f"{ME}/mowers/{mower_id}",
            json={
                "installation_id": str(installation),
                "activity_id": "ABC-123",
                "push_token": UPDATE_TOKEN,
            },
            headers=owner.headers,
        )
        h.clock.advance(5)
        await watcher.on_state(mowing(mower_id, owner.account_id, 9, at=h.clock.now))
        too_soon = len(recorder.pushes)
        h.clock.advance(20)
        await watcher.on_state(mowing(mower_id, owner.account_id, 12, at=h.clock.now))
        await watcher.on_state(mowing(mower_id, owner.account_id, 12, at=h.clock.now))  # same
        h.clock.advance(60)
        await watcher.on_state(
            make_state(mower_id, owner.account_id, at=h.clock.now, activity=MowerActivity.DOCKED)
        )
        after_end = len(recorder.pushes)
        await watcher.on_state(
            make_state(mower_id, owner.account_id, at=h.clock.now, activity=MowerActivity.DOCKED)
        )
        rows = await h.store.live_activities(mower_id)
    assert registered.json()["has_push_to_start_token"] is True
    assert START_TOKEN not in registered.text
    assert len(started) == 1 and started[0]["event"] == "start"
    assert started[0]["token"] == START_TOKEN and started[0]["push_type"] == "liveactivity"
    aps = started[0]["payload"]["aps"]
    assert aps["attributes-type"] == "MowingActivityAttributes"
    assert aps["attributes"]["mowerName"] == "Luba"
    assert aps["content-state"]["state"] == "mowing"
    assert aps["content-state"]["progressPercent"] == 5
    assert aps["content-state"]["areaName"] == "Großer Garten"
    assert report.status_code == 200 and report.json()["state"] == "running"
    assert UPDATE_TOKEN not in report.text
    assert too_soon == 1  # 5 s later: rate limited
    updates = [p for p in recorder.pushes if p["event"] == "update"]
    assert len(updates) == 1  # the unchanged state sent nothing
    assert updates[0]["token"] == UPDATE_TOKEN
    assert updates[0]["payload"]["aps"]["content-state"]["progressPercent"] == 12
    assert "stale-date" in updates[0]["payload"]["aps"]
    ends = [p for p in recorder.pushes if p["event"] == "end"]
    assert len(ends) == 1 and after_end == len(recorder.pushes)
    assert ends[0]["payload"]["aps"]["content-state"]["state"] == "finished"
    assert ends[0]["payload"]["aps"]["dismissal-date"] > ends[0]["payload"]["aps"]["timestamp"]
    assert rows == []


async def test_an_error_ends_the_activity_as_error_and_a_dismissed_one_is_not_restarted() -> None:
    recorder = ApnsRecorder()
    async with harness(channels={"apns": recorder}) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        first, second = uuid4(), uuid4()
        await register(h, owner, first)
        await register(h, owner, second, push_to_start_token=None)
        watcher = h.services.watcher
        await watcher.on_state(mowing(mower_id, owner.account_id, 1))
        for installation in (first,):
            await h.client.put(
                f"{ME}/mowers/{mower_id}",
                json={
                    "installation_id": str(installation),
                    "activity_id": "A-1",
                    "push_token": UPDATE_TOKEN,
                },
                headers=owner.headers,
            )
        dismissed = await h.client.delete(
            f"{ME}/mowers/{mower_id}?installation_id={first}", headers=owner.headers
        )
        h.clock.advance(60)
        await watcher.on_state(mowing(mower_id, owner.account_id, 30, at=h.clock.now))
        count = len(recorder.pushes)
        h.clock.advance(60)
        await watcher.on_state(
            make_state(mower_id, owner.account_id, at=h.clock.now, activity=MowerActivity.ERROR)
        )
    assert dismissed.status_code == 204
    assert count == 1  # only the first start; a dismissed activity gets nothing more
    assert len(recorder.pushes) == 1  # and no end push for it either


async def test_error_final_state_and_dead_tokens() -> None:
    recorder = ApnsRecorder()
    async with harness(channels={"apns": recorder}) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        installation = uuid4()
        await register(h, owner, installation)
        watcher = h.services.watcher
        await watcher.on_state(mowing(mower_id, owner.account_id, 1))
        await h.client.put(
            f"{ME}/mowers/{mower_id}",
            json={
                "installation_id": str(installation),
                "activity_id": "A-1",
                "push_token": UPDATE_TOKEN,
            },
            headers=owner.headers,
        )
        h.clock.advance(60)
        await watcher.on_state(
            make_state(mower_id, owner.account_id, at=h.clock.now, activity=MowerActivity.ERROR)
        )
        end = recorder.pushes[-1]
        # A dead update token drops the row; a dead push-to-start token is forgotten.
        other = await h.add_mower(owner)
        recorder.results = [DeliveryResult(DeliveryOutcome.GONE, "Unregistered")]
        await watcher.on_state(mowing(other, owner.account_id, 1))
        devices = await h.store.activity_devices(owner.account_id)
    assert end["event"] == "end"
    assert end["payload"]["aps"]["content-state"]["state"] == "error"
    assert end["payload"]["aps"]["dismissal-date"] - end["payload"]["aps"]["timestamp"] == 1800
    assert devices[0].push_to_start_token is None


async def test_only_users_who_may_view_the_mower_get_activities() -> None:
    recorder = ApnsRecorder()
    async with harness(channels={"apns": recorder}) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        member = await h.member(
            owner, perms=["mower.view", "notifications.manage"]
        )  # no grant on the mower
        await register(h, owner, uuid4())
        await register(h, member, uuid4())
        report = await h.client.put(
            f"{ME}/mowers/{mower_id}",
            json={
                "installation_id": str(uuid4()),
                "activity_id": "A-1",
                "push_token": UPDATE_TOKEN,
            },
            headers=member.headers,
        )
        await h.services.watcher.on_state(mowing(mower_id, owner.account_id, 1))
    assert report.status_code == 404
    assert len(recorder.pushes) == 1  # the owner's installation only


async def test_widget_installations_get_one_rate_limited_background_push_per_change() -> None:
    recorder = ApnsRecorder()
    async with harness(channels={"apns": recorder}) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        installation = uuid4()
        await register(h, owner, installation, push_to_start_token=None, widgets_enabled=True)
        await h.client.post(
            f"{API}/me/push/apns",
            json={"token": DEVICE_TOKEN, "installation_id": str(installation)},
            headers=owner.headers,
        )
        other = await h.client.post(
            f"{API}/me/push/apns", json={"token": "d4" * 32}, headers=owner.headers
        )
        watcher = h.services.watcher
        docked = make_state(mower_id, owner.account_id, activity=MowerActivity.DOCKED)
        await watcher.on_state(docked)  # first state: nothing changed yet
        h.clock.advance(1)
        await watcher.on_state(mowing(mower_id, owner.account_id, 1, at=h.clock.now))
        h.clock.advance(1)
        await watcher.on_state(docked.model_copy(update={"observed_at": h.clock.now}))
        h.clock.advance(300)
        await watcher.on_state(mowing(mower_id, owner.account_id, 2, at=h.clock.now))
    assert other.status_code == 201
    pushes = [p for p in recorder.pushes if p["push_type"] == "background"]
    assert [p["token"] for p in pushes] == [DEVICE_TOKEN, DEVICE_TOKEN]  # 2nd change too soon
    assert pushes[0]["payload"]["aps"] == {"content-available": 1}
    assert pushes[0]["payload"]["kind"] == "widget-reload"
    assert pushes[0]["priority"] == 5


async def test_registration_validates_and_isolates_users() -> None:
    async with harness(channels={"apns": ApnsRecorder()}) as h:
        owner = await h.owner()
        other = await h.owner()
        installation = uuid4()
        bad_token = await h.client.put(
            f"{ME}/device",
            json={"installation_id": str(installation), "push_to_start_token": "zz" * 40},
            headers=owner.headers,
        )
        anonymous = await h.client.put(f"{ME}/device", json={"installation_id": str(installation)})
        await register(h, owner, installation)
        foreign_delete = await h.client.delete(f"{ME}/device/{installation}", headers=other.headers)
        own_delete = await h.client.delete(f"{ME}/device/{installation}", headers=owner.headers)
        again = await h.client.delete(f"{ME}/device/{installation}", headers=owner.headers)
        bad_activity = await h.client.put(
            f"{ME}/mowers/{uuid4()}",
            json={"installation_id": str(installation), "activity_id": "x y", "push_token": "11"},
            headers=owner.headers,
        )
    assert bad_token.status_code == 422
    assert anonymous.status_code == 401
    assert foreign_delete.status_code == 404
    assert own_delete.status_code == 204 and again.status_code == 404
    assert bad_activity.status_code == 422


async def test_without_the_apns_channel_nothing_happens_and_registration_still_works() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await register(h, owner, uuid4())
        await h.services.watcher.on_state(mowing(mower_id, owner.account_id, 1))
        rows = await h.store.live_activities(mower_id)
    assert rows == []
