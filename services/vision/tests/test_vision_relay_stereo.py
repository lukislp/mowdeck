"""Both front cameras through the camera relay (ADR 0045): the gateway's photo of a mower without
own stills is a frame set of the left and right front camera of the same moment. Vision asks for
it (``max_frames`` 2) and analyses it as a stereo pair, for a manual analysis and for the
automatic analysis of an error; a set of one frame stays a plain photo; safety stays strict."""

import base64
import io
from datetime import UTC, datetime, timedelta
from typing import Any

from mowdeck_contracts.v1 import MowerActivity, PhotoFrame, PhotoReply, PhotoRequest, VisionTask
from mowdeck_contracts.v1.relay import VisionDemand
from mowdeck_service_kit.bus import BusMessage
from PIL import Image as PilImage
from vision_support import API, Harness, Person, SimRig, answer, harness, wait_for

from vision.adapters.bus.gateways import _images
from vision.adapters.providers.prompt import MULTI_IMAGE_SAFETY, STEREO_HINT, user_content
from vision.domain.context import Motion, SceneContext
from vision.domain.frames import stereo_pair
from vision.domain.models import Image, ProposalState, RiskFlag

AT = datetime(2026, 10, 18, 12, 0, 0, 250_000, tzinfo=UTC)


def _jpeg(level: int) -> bytes:
    buffer = io.BytesIO()
    PilImage.new("RGB", (64, 48), (level, level, 120)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _frame(side: str, name: str, at: datetime, level: int) -> PhotoFrame:
    return PhotoFrame(
        image_base64=base64.b64encode(_jpeg(level)).decode(),
        media_type="image/jpeg",
        captured_at=at,
        camera_id=f"front_{side}",
        camera_name=name,
        side=side,  # type: ignore[arg-type]
    )


LEFT = _frame("left", "Front left", AT, 150)
RIGHT = _frame("right", "Front right", AT + timedelta(milliseconds=60), 170)


class RelayRig(SimRig):
    """A gateway whose mower takes no stills itself: its photo is the relay's frame set."""

    def __init__(self, *args: Any, frames: list[PhotoFrame], **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.frames = frames
        self.missing: list[str] = []
        self.requests: list[PhotoRequest] = []

    async def _photo(self, message: BusMessage) -> None:
        request = message.model(PhotoRequest)
        if request.account_id != self.account_id:
            return
        self.requests.append(request)
        frames = self.frames[: request.max_frames]
        await message.respond(PhotoReply(frames=frames, frames_missing=self.missing))


async def _relay_rig(
    h: Harness,
    owner: Person,
    frames: list[PhotoFrame],
    cls: type[RelayRig] = RelayRig,
    **kw: Any,
) -> RelayRig:
    mower_id = h.mower(owner)
    rig = cls(h.bus, mower_id=mower_id, account_id=owner.account_id, frames=frames, **kw)
    rig.idle_at()
    await rig.start()
    h.rigs.append(rig)
    await wait_for(lambda: h.service.view.state(mower_id) is not None)
    return rig


async def _stuck(h: Harness, rig: SimRig) -> None:
    rig.sim.inject_stuck()
    await wait_for(
        lambda: (
            (o := h.service.view.state(rig.mower_id)) is not None
            and o.state.activity is MowerActivity.ERROR
        )
    )


async def _analyse(h: Harness, owner: Person, rig: SimRig) -> dict[str, Any]:
    response = await h.client.post(
        f"{API}/mowers/{rig.mower_id}/recover", json={"task": "recover"}, headers=owner.headers
    )
    assert response.status_code == 202, response.text
    body = await h.until(response.json()["id"], owner, *ProposalState)
    await h.service.idle()
    return await h.proposal(body["id"], owner)


def _frames(proposal: dict[str, Any]) -> list[tuple[int, str, str | None, str | None]]:
    return [(f["index"], f["source"], f["side"], f["camera"]) for f in proposal["frames"]]


async def _pixel(h: Harness, owner: Person, proposal: dict[str, Any], index: int) -> int:
    image = await h.client.get(
        f"{API}/proposals/{proposal['id']}/images/{index}", headers=owner.headers
    )
    assert image.status_code == 200
    pixel = PilImage.open(io.BytesIO(image.content)).convert("RGB").getpixel((5, 5))
    return int(pixel[0])  # type: ignore[index]


async def test_vision_relay_pair_of_a_manual_analysis_is_a_stereo_proposal() -> None:
    async with harness() as h:
        owner = h.owner()
        rig = await _relay_rig(h, owner, [RIGHT, LEFT])  # in any order from the gateway
        await _stuck(h, rig)
        proposal = await _analyse(h, owner, rig)
        assert proposal["state"] == ProposalState.PROPOSED.value
        assert proposal["stereo"] is True
        assert proposal["image_count"] == 2
        assert _frames(proposal) == [
            (0, "mower", "left", "Front left"),
            (1, "mower", "right", "Front right"),
        ]
        assert proposal["frames"][0]["captured_at"].startswith("2026-10-18T12:00:00.25")
        _, count, context, _ = h.factory.calls[-1]
        assert count == 2
        assert context.stereo is True and context.images == 2
        assert abs(await _pixel(h, owner, proposal, 0) - 150) < 8, "left first"
        assert abs(await _pixel(h, owner, proposal, 1) - 170) < 8
        [request] = rig.requests
        assert request.max_frames == 2, "Vision asks for both front cameras"
        assert request.demand is VisionDemand.ERROR_ANALYSIS
        request_log = h.store.requests[-1]
        assert request_log.detail is not None and "stereo=true" in request_log.detail


async def test_vision_relay_pair_of_the_automatic_error_analysis_is_stereo_too() -> None:
    async with harness() as h:
        owner = h.owner()
        saved = await h.client.put(
            f"{API}/me/preferences", json={"auto_analysis": True}, headers=owner.headers
        )
        assert saved.status_code == 200, saved.text
        rig = await _relay_rig(h, owner, [LEFT, RIGHT])
        rig.sim.inject_stuck()
        await wait_for(lambda: len(h.store.proposals) == 1)
        [proposal_id] = list(h.store.proposals)
        body = await h.until(str(proposal_id), owner, ProposalState.PROPOSED)
        await h.service.idle()
        assert body["trigger"] == "error"
        assert body["stereo"] is True
        assert [f["side"] for f in body["frames"]] == ["left", "right"]
        assert h.factory.calls[-1][2].stereo is True


async def test_vision_relay_set_of_one_frame_stays_a_plain_photo() -> None:
    async with harness() as h:
        owner = h.owner()
        rig = await _relay_rig(h, owner, [LEFT])
        rig.missing = ["front_right"]
        await _stuck(h, rig)
        proposal = await _analyse(h, owner, rig)
        assert proposal["stereo"] is False
        assert proposal["image_count"] == 1
        assert _frames(proposal) == [(0, "mower", "left", "Front left")]
        assert h.factory.calls[-1][2].stereo is False


async def test_vision_relay_frames_too_far_apart_are_two_plain_images() -> None:
    async with harness() as h:
        owner = h.owner()
        late = _frame("right", "Front right", AT + timedelta(milliseconds=900), 170)
        rig = await _relay_rig(h, owner, [LEFT, late])
        await _stuck(h, rig)
        proposal = await _analyse(h, owner, rig)
        assert proposal["stereo"] is False
        assert proposal["image_count"] == 2


async def test_vision_relay_pair_with_the_stereo_preference_off_keeps_the_left_frame() -> None:
    async with harness() as h:
        owner = h.owner()
        saved = await h.client.put(
            f"{API}/me/preferences", json={"stereo_frames": False}, headers=owner.headers
        )
        assert saved.status_code == 200, saved.text
        rig = await _relay_rig(h, owner, [LEFT, RIGHT])
        await _stuck(h, rig)
        proposal = await _analyse(h, owner, rig)
        assert proposal["stereo"] is False
        assert [f["side"] for f in proposal["frames"]] == ["left"]


async def test_vision_relay_a_person_in_either_image_aborts_the_pair() -> None:
    async with harness() as h:
        h.factory.default = answer(risk=[RiskFlag.PEOPLE_VISIBLE])
        owner = h.owner()
        rig = await _relay_rig(h, owner, [LEFT, RIGHT])
        await _stuck(h, rig)
        proposal = await _analyse(h, owner, rig)
        assert proposal["stereo"] is True
        assert proposal["state"] == ProposalState.REJECTED_BY_AI.value
        assert h.fleet.starts == [], "nothing is driven"


def test_vision_relay_pair_needs_frames_of_one_source() -> None:
    left = Image(_jpeg(150), "image/jpeg", "mower", "Front left", "left", AT)
    right = Image(_jpeg(170), "image/jpeg", "mower", "Front right", "right", AT)
    upload = Image(_jpeg(170), "image/jpeg", "upload", "Front right", "right", AT)
    assert stereo_pair([right, left]) == [left, right]
    assert stereo_pair([left, upload]) == [], "a mower frame and an upload are no pair"


def test_vision_prompt_keeps_every_image_a_safety_check_of_its_own() -> None:
    context = SceneContext(
        task=VisionTask.RECOVER,
        motion=Motion(linear_mps_at_1=0.5, turn_deg_per_s_at_1=57.0),
        images=2,
        stereo=True,
    )
    left = Image(_jpeg(150), "image/jpeg", "mower", "Front left", "left", AT)
    right = Image(_jpeg(170), "image/jpeg", "mower", "Front right", "right", AT)
    text = user_content([left, right], context, VisionTask.RECOVER)[0]["text"]
    assert STEREO_HINT in text
    assert MULTI_IMAGE_SAFETY in text
    assert "any one image" in MULTI_IMAGE_SAFETY
    assert "Never let one image overrule" in MULTI_IMAGE_SAFETY
    single = SceneContext(
        task=VisionTask.RECOVER,
        motion=Motion(linear_mps_at_1=0.5, turn_deg_per_s_at_1=57.0),
        images=1,
    )
    assert MULTI_IMAGE_SAFETY not in user_content([left], single, VisionTask.RECOVER)[0]["text"]


class NightRelayRig(RelayRig):
    """The relay's pair is dark until the mower reports its light on."""

    async def _photo(self, message: BusMessage) -> None:
        level = 200 if self.sim.snapshot().light_on else 10
        self.frames = [
            _frame("left", "Front left", AT, level),
            _frame("right", "Front right", AT + timedelta(milliseconds=40), level),
        ]
        await super()._photo(message)


async def test_vision_relay_dark_pair_is_retaken_as_a_pair_with_the_light_on() -> None:
    async with harness() as h:
        owner = h.owner()
        rig = await _relay_rig(h, owner, [], cls=NightRelayRig)
        await wait_for(lambda: h.service.view.capabilities(rig.mower_id) is not None)
        await _stuck(h, rig)
        proposal = await _analyse(h, owner, rig)
        assert proposal["light_used"] is True
        assert proposal["stereo"] is True
        assert [f["side"] for f in proposal["frames"]] == ["left", "right"]
        assert await _pixel(h, owner, proposal, 0) > 150, "the model saw the lit pair"
        assert len(rig.requests) == 2


def test_vision_photo_reply_images_of_both_reply_shapes() -> None:
    single = PhotoReply(image_base64=base64.b64encode(_jpeg(90)).decode(), media_type="image/png")
    [image] = _images(single)
    assert (image.source, image.media_type, image.side, image.camera) == (
        "mower",
        "image/png",
        None,
        None,
    )
    broken = LEFT.model_copy(update={"image_base64": "not base64!"})
    images = _images(PhotoReply(frames=[broken, RIGHT]))
    assert [(i.side, i.camera, i.captured_at) for i in images] == [
        ("right", "Front right", RIGHT.captured_at)
    ]
    unnamed = RIGHT.model_copy(update={"camera_name": None})
    assert _images(PhotoReply(frames=[unnamed]))[0].camera == "front_right"
