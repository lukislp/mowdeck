"""The automatic analysis gets its photo or says why it has none (live case 2026-10-24): a LUBA
on weak Wi-Fi (-79 dBm) stopped with error 1201; the camera relay's photo request timed out
(``frame-timeout``) and the automatic analysis was skipped silently (``no_photo``). Now the
photo is asked for again within a bounded time, a stereo pair is preferred over a single
camera, and without any photo a ``failed`` proposal records the reason, which Notifications
sends to the owner. A manual request with a single camera frame gets the gateway's stereo pair,
and a manual request without any photo says why the model saw only the map."""

import base64
import io
from datetime import UTC, datetime, timedelta
from typing import Any

from mowdeck_contracts.v1 import (
    VISION_PROPOSAL_UPDATED,
    ConnectionKind,
    EventEnvelope,
    MowerActivity,
    MowerState,
    NetworkKind,
    PhotoError,
    PhotoErrorCode,
    PhotoFrame,
    PhotoReply,
    PhotoRequest,
    RtkFixStatus,
    SignalStatus,
    VisionPhotoProblem,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit.bus import BusMessage
from PIL import Image as PilImage
from test_vision_auto import _store_opt_in
from vision_support import API, Harness, Person, SimRig, harness, wait_for

from vision.domain.models import ProposalState
from vision.domain.photo import diagnose

QUICK: dict[str, Any] = {
    "vision_photo_timeout_seconds": 0.5,
    "vision_auto_photo_retry_seconds": 0.05,
    "vision_auto_photo_attempts": 3,
    "vision_auto_photo_window_seconds": 30.0,
}
RELAY_TIMEOUT = "the camera relay answered 504 frame-timeout: no camera frame in time"


def _jpeg(level: int) -> bytes:
    buffer = io.BytesIO()
    PilImage.new("RGB", (64, 48), (level, level, 120)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _frame(side: str, at: datetime) -> PhotoFrame:
    return PhotoFrame(
        image_base64=base64.b64encode(_jpeg(150)).decode(),
        media_type="image/jpeg",
        captured_at=at,
        camera_id=f"front_{side}",
        camera_name=f"Front {side}",
        side=side,  # type: ignore[arg-type]
    )


class ScriptedRig(SimRig):
    """A gateway whose photo answers follow a script, one entry per request (the last one
    repeats): ``error`` (the relay's frame-timeout), ``silent`` (no answer: a bus timeout),
    ``left`` (only the left camera of the pair) or ``pair``. ``wifi_dbm`` sets the signal the
    mower reports."""

    def __init__(self, *args: Any, script: list[str], wifi_dbm: int = -45, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.script = script
        self.wifi_dbm = wifi_dbm
        self.requests: list[PhotoRequest] = []

    def _signal(self, state: MowerState) -> MowerState:
        signal = SignalStatus(wifi_dbm=self.wifi_dbm, network=NetworkKind.WIFI)
        return state.model_copy(update={"signal": signal})

    async def _state(self, state: MowerState) -> None:
        await super()._state(self._signal(state))

    async def _heartbeat(self) -> None:
        import asyncio

        from mowdeck_contracts.v1.subjects import telemetry_subject

        while True:
            await asyncio.sleep(0.25)
            await self.bus.publish(
                telemetry_subject(self.mower_id), self._signal(self.sim.snapshot())
            )

    async def _photo(self, message: BusMessage) -> None:
        request = message.model(PhotoRequest)
        if request.account_id != self.account_id:
            return
        self.requests.append(request)
        step = self.script[min(len(self.requests), len(self.script)) - 1]
        now = datetime.now(UTC)
        if step == "silent":
            return
        if step == "error":
            reply = PhotoReply(error=PhotoError(code=PhotoErrorCode.FAILED, detail=RELAY_TIMEOUT))
        elif step == "left":
            reply = PhotoReply(frames=[_frame("left", now)], frames_missing=["front_right"])
        else:
            right = _frame("right", now + timedelta(milliseconds=40))
            reply = PhotoReply(frames=[_frame("left", now), right])
        await message.respond(reply)


async def _rig(h: Harness, owner: Person, script: list[str], **kw: Any) -> ScriptedRig:
    mower_id = h.mower(owner)
    rig = ScriptedRig(h.bus, mower_id=mower_id, account_id=owner.account_id, script=script, **kw)
    rig.idle_at()
    await rig.start()
    h.rigs.append(rig)
    await wait_for(lambda: h.service.view.state(mower_id) is not None)
    return rig


async def _stuck(h: Harness, rig: ScriptedRig) -> MowerState:
    rig.sim.inject_stuck()
    await wait_for(
        lambda: (
            (o := h.service.view.state(rig.mower_id)) is not None
            and o.state.activity is MowerActivity.ERROR
        )
    )
    observed = h.service.view.state(rig.mower_id)
    assert observed is not None
    return observed.state.model_copy(update={"observed_at": datetime.now(UTC)})


def _updates(h: Harness) -> list[dict[str, Any]]:
    return [
        EventEnvelope[dict[str, Any]].model_validate_json(m.data).payload
        for m in h.bus.published
        if m.subject == event_subject_for_type(VISION_PROPOSAL_UPDATED)
    ]


# --- the diagnosis -----------------------------------------------------------------------------


def _state(**changes: Any) -> MowerState:
    values: dict[str, Any] = {
        "mower_id": "11111111-1111-4111-8111-111111111111",
        "account_id": "22222222-2222-4222-8222-222222222222",
        "observed_at": datetime(2026, 10, 24, 12, 14, 57, tzinfo=UTC),
        "activity": MowerActivity.ERROR,
        "error_code": "1201",
        "battery_percent": 80,
        "rtk_fix": RtkFixStatus.FIXED,
        "connection": ConnectionKind.CLOUD,
        **changes,
    }
    return MowerState.model_validate(values)


def test_diagnosis_names_the_weak_wifi_of_the_live_case() -> None:
    live = _state(signal={"network": "wifi", "wifi_dbm": -79, "mobile_dbm": -77})
    found = diagnose(live, f"failed: {RELAY_TIMEOUT}")
    assert found.problem is VisionPhotoProblem.WEAK_WIFI and found.signal_dbm == -79
    text = found.text(3)
    assert text.startswith("no photo after 3 attempts: weak Wi-Fi (-79 dBm)")
    assert "start the analysis by hand" in text


def test_diagnosis_other_causes() -> None:
    good = _state(signal={"network": "wifi", "wifi_dbm": -45})
    assert diagnose(good, "timeout").problem is VisionPhotoProblem.NO_FRAME
    mobile = _state(signal={"network": "mobile", "wifi_dbm": -90, "mobile_dbm": -110})
    found = diagnose(mobile)
    assert found.problem is VisionPhotoProblem.WEAK_MOBILE and found.signal_dbm == -110
    strong_mobile = _state(signal={"network": "mobile", "wifi_dbm": -90, "mobile_dbm": -80})
    assert diagnose(strong_mobile).problem is VisionPhotoProblem.NO_FRAME
    assert diagnose(None).problem is VisionPhotoProblem.OFFLINE
    assert diagnose(_state(activity="offline")).problem is VisionPhotoProblem.OFFLINE
    assert diagnose(good, "not_connected: gone").problem is VisionPhotoProblem.OFFLINE
    ble = _state(signal={"network": "ble"})
    assert diagnose(ble).problem is VisionPhotoProblem.OFFLINE
    assert diagnose(_state()).problem is VisionPhotoProblem.NO_FRAME


# --- the automatic analysis (the real consumer: the opted-in owner's mower gets stuck) ---------


async def _auto_round(h: Harness, rig: ScriptedRig, *states: ProposalState) -> Any:
    """Get the mower stuck and wait for the automatic analysis's proposal to reach a state."""
    await _stuck(h, rig)
    await wait_for(lambda: any(p.state in states for p in h.store.proposals.values()), timeout=15)
    await h.service.idle()
    [proposal] = h.store.proposals.values()
    return proposal


async def test_auto_retries_until_the_photo_comes_and_prefers_the_pair() -> None:
    async with harness(**QUICK) as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await _rig(h, owner, ["error", "left", "pair"])
        proposal = await _auto_round(h, rig, ProposalState.PROPOSED)
        assert len(rig.requests) == 3, "frame-timeout, one camera only, then the pair"
        body = await h.proposal(str(proposal.id), owner)
        assert body["trigger"] == "error"
        assert body["stereo"] is True and body["image_count"] == 2
        assert [f["side"] for f in body["frames"]] == ["left", "right"]
        assert body["photo_problem"] is None


async def test_auto_takes_one_camera_on_the_last_attempt() -> None:
    async with harness(**QUICK) as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await _rig(h, owner, ["left"])
        proposal = await _auto_round(h, rig, ProposalState.PROPOSED)
        assert len(rig.requests) == 3
        body = await h.proposal(str(proposal.id), owner)
        assert body["stereo"] is False and body["image_count"] == 1


async def test_auto_without_photo_records_why_and_tells_the_owner() -> None:
    async with harness(**QUICK) as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await _rig(h, owner, ["error", "silent", "error"], wifi_dbm=-79)
        stored = await _auto_round(h, rig, ProposalState.FAILED)
        assert len(rig.requests) == 3
        assert h.factory.calls == [], "no model without a photo"
        assert stored.image_count == 0
        assert stored.detail is not None
        assert stored.detail.startswith("no photo after 3 attempts: weak Wi-Fi (-79 dBm)")
        body = await h.proposal(str(stored.id), owner)
        assert body["state"] == "failed" and body["final"] is True
        assert body["trigger"] == "error" and body["trigger_error_code"] == "stuck"
        assert body["photo_problem"] == "weak_wifi" and body["signal_dbm"] == -79
        [update] = _updates(h)
        assert update["state"] == "failed" and update["final"] is True
        assert update["trigger"] == "error" and update["error_code"] == "stuck"
        assert update["photo_problem"] == "weak_wifi" and update["signal_dbm"] == -79
        assert update["link"] == f"/mowers/{rig.mower_id}?assist={stored.id}"
        assert h.fleet.starts == [] and h.fleet.issued == []


async def test_auto_stops_asking_once_the_error_is_gone() -> None:
    async with harness(**{**QUICK, "vision_auto_photo_retry_seconds": 0.4}) as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await _rig(h, owner, ["error"])
        await _stuck(h, rig)
        await wait_for(lambda: len(rig.requests) == 1)
        rig.idle_at()  # the mower freed itself while the analysis waited
        await wait_for(lambda: len(h.store.proposals) == 1, timeout=10)
        await h.service.idle()
        assert len(rig.requests) < 3
        [stored] = h.store.proposals.values()
        assert stored.state is ProposalState.FAILED


async def test_auto_retries_stay_within_the_window() -> None:
    settings: dict[str, Any] = {
        **QUICK,
        "vision_auto_photo_attempts": 10,
        "vision_auto_photo_window_seconds": 0.0,
    }
    async with harness(**settings) as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await _rig(h, owner, ["error"])
        await _auto_round(h, rig, ProposalState.FAILED)
        assert len(rig.requests) == 1


# --- manual requests ---------------------------------------------------------------------------


async def test_manual_single_camera_frame_becomes_the_stereo_pair() -> None:
    async with harness(**QUICK) as h:
        owner = h.owner()
        rig = await _rig(h, owner, ["pair"])
        await _stuck(h, rig)
        single = {
            "media_type": "image/jpeg",
            "data": base64.b64encode(_jpeg(90)).decode(),
            "camera": "Front left",
        }
        response = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover",
            json={"task": "recover", "images": [single]},
            headers=owner.headers,
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["stereo"] is True and body["image_count"] == 2
        assert [f["source"] for f in body["frames"]] == ["mower", "mower"]
        assert len(rig.requests) == 1


async def test_manual_single_frame_stays_when_the_gateway_has_no_pair() -> None:
    async with harness(**QUICK) as h:
        owner = h.owner()
        rig = await _rig(h, owner, ["error"])
        await _stuck(h, rig)
        single = {
            "media_type": "image/jpeg",
            "data": base64.b64encode(_jpeg(90)).decode(),
            "camera": "Front left",
        }
        response = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover",
            json={"task": "recover", "images": [single]},
            headers=owner.headers,
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["stereo"] is False and body["image_count"] == 1
        assert body["frames"][0]["source"] == "upload"


async def test_manual_without_photo_says_why_the_model_saw_only_the_map() -> None:
    async with harness(**QUICK, vision_map_image=True) as h:
        owner = h.owner()
        rig = await _rig(h, owner, ["error"], wifi_dbm=-79)
        await _stuck(h, rig)
        response = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover", json={"task": "recover"}, headers=owner.headers
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert [f["source"] for f in body["frames"]] == ["map"]
        assert body["photo_problem"] == "weak_wifi" and body["signal_dbm"] == -79
        await wait_for(lambda: len(h.factory.calls) == 1)
        _, _, context, _ = h.factory.calls[0]
        assert context.photo_problem is VisionPhotoProblem.WEAK_WIFI
