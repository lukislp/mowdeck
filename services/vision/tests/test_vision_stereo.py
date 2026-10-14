"""Stereo frames: the left and right front camera of the same moment as one pair for the model."""

import base64
import io
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from mowdeck_contracts.v1 import MowerActivity, VisionTask
from PIL import Image as PilImage
from vision_support import API, Harness, harness, wait_for

from vision.adapters.memory import MemoryStore
from vision.adapters.providers.prompt import STEREO_HINT, SYSTEM_PROMPT, image_label, user_content
from vision.domain.context import Motion, SceneContext
from vision.domain.frames import capture_skew_ms, stereo_pair
from vision.domain.models import FrameInfo, Image, ProposalState, UserPreferences

AT = datetime(2026, 10, 11, 12, 0, 0, 120_000, tzinfo=UTC)


def _jpeg(level: int) -> bytes:
    buffer = io.BytesIO()
    PilImage.new("RGB", (64, 48), (level, level, 120)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _frame(side: str | None, camera: str | None, at: datetime | None, level: int = 150) -> Image:
    return Image(_jpeg(level), "image/jpeg", "upload", camera, side, at)  # type: ignore[arg-type]


LEFT = _frame("left", "Front left", AT)
RIGHT = _frame("right", "Front right", AT + timedelta(milliseconds=80), 160)


# --- domain ---------------------------------------------------------------------------------


def test_vision_stereo_pair_orders_left_first_and_checks_the_skew() -> None:
    assert stereo_pair([RIGHT, LEFT]) == [LEFT, RIGHT]
    assert capture_skew_ms(LEFT, RIGHT) == 80
    late = _frame("right", "Front right", AT + timedelta(milliseconds=900))
    assert stereo_pair([LEFT, late]) == [], "too far apart for the same moment"
    assert stereo_pair([LEFT, late], max_skew_ms=1000) == [LEFT, late]
    untimed = [_frame("left", "Front left", None), _frame("right", "Front right", None)]
    assert stereo_pair(untimed) == untimed, "no times: accepted as sent"
    assert capture_skew_ms(untimed[0], untimed[1]) is None


def test_vision_stereo_pair_needs_two_sides_two_cameras_and_uploads() -> None:
    assert stereo_pair([LEFT]) == []
    assert stereo_pair([LEFT, RIGHT, LEFT]) == []
    assert stereo_pair([LEFT, _frame("left", "Front right", AT)]) == []
    assert stereo_pair([LEFT, _frame(None, "Front right", AT)]) == []
    same_camera = _frame("right", "Front left", AT)
    assert stereo_pair([LEFT, same_camera]) == [], "the same camera twice is no pair"
    mower = Image(_jpeg(100), "image/jpeg", "mower", "Front right", "right", AT)
    assert stereo_pair([LEFT, mower]) == []


def test_vision_frame_info_round_trips_through_json() -> None:
    info = LEFT.info(0)
    assert info == FrameInfo(0, "upload", "Front left", "left", AT)
    assert FrameInfo.from_json(info.to_json()) == info
    odd = FrameInfo.from_json({"index": 3, "source": "x", "side": "up", "camera": 7})
    assert odd == FrameInfo(3, "upload", None, None, None)


# --- prompt ---------------------------------------------------------------------------------


def _context(*, stereo: bool, images: int = 2) -> SceneContext:
    return SceneContext(
        task=VisionTask.RECOVER,
        motion=Motion(linear_mps_at_1=0.5, turn_deg_per_s_at_1=57.0),
        images=images,
        stereo=stereo,
    )


def test_vision_prompt_shows_the_pair_side_by_side_with_the_parallax_hint() -> None:
    parts = user_content([LEFT, RIGHT], _context(stereo=True), VisionTask.RECOVER)
    kinds = [p["type"] for p in parts]
    assert kinds == ["text", "text", "image_url", "text", "image_url"]
    assert STEREO_HINT in parts[0]["text"]
    assert '"stereo":true' in parts[0]["text"]
    assert "parallax" in STEREO_HINT
    assert "do not invent a distance" in STEREO_HINT
    assert parts[1]["text"] == "Image 1: Front left camera (left) captured 12:00:00.120 UTC"
    assert parts[3]["text"].startswith("Image 2: Front right camera (right)")
    assert "parallax" in SYSTEM_PROMPT


def test_vision_prompt_without_stereo_has_no_hint_and_plain_images_no_caption() -> None:
    plain = Image(_jpeg(150), "image/jpeg")
    parts = user_content([plain], _context(stereo=False, images=1), VisionTask.RECOVER)
    assert [p["type"] for p in parts] == ["text", "image_url"]
    assert STEREO_HINT not in parts[0]["text"]
    assert image_label(0, plain) is None
    assert image_label(1, _frame(None, "Front left", None)) == "Image 2: Front left camera"


# --- API and use case -----------------------------------------------------------------------


def _wire(image: Image) -> dict[str, Any]:
    body: dict[str, Any] = {
        "media_type": image.media_type,
        "data": base64.b64encode(image.data).decode(),
    }
    if image.camera:
        body["camera"] = image.camera
    if image.side:
        body["side"] = image.side
    if image.captured_at:
        body["captured_at"] = image.captured_at.isoformat()
    return body


async def _request(h: Harness, images: list[Image], **rig: Any) -> tuple[Any, dict[str, Any]]:
    owner = h.owner()
    mower = await h.rig(owner, **rig)
    mower.sim.inject_stuck()
    await wait_for(
        lambda: (
            (o := h.service.view.state(mower.mower_id)) is not None
            and o.state.activity is MowerActivity.ERROR
        )
    )
    response = await h.client.post(
        f"{API}/mowers/{mower.mower_id}/recover",
        json={"task": "recover", "images": [_wire(i) for i in images]},
        headers=owner.headers,
    )
    assert response.status_code == 202, response.text
    done = await h.until(response.json()["id"], owner, ProposalState.PROPOSED)
    await h.service.idle()
    return owner, done


async def test_vision_stereo_request_stores_both_frames_and_flags_the_proposal() -> None:
    async with harness() as h:
        owner, proposal = await _request(h, [RIGHT, LEFT])
        assert proposal["stereo"] is True
        assert proposal["image_count"] == 2
        assert [(f["index"], f["side"], f["camera"]) for f in proposal["frames"]] == [
            (0, "left", "Front left"),
            (1, "right", "Front right"),
        ]
        assert proposal["frames"][0]["source"] == "upload"
        assert proposal["light_used"] is False, "uploaded frames are analysed as they are"
        _, count, context, _ = h.factory.calls[-1]
        assert count == 2
        assert context.stereo is True
        assert context.images == 2
        for index, level in ((0, 150), (1, 160)):
            image = await h.client.get(
                f"{API}/proposals/{proposal['id']}/images/{index}", headers=owner.headers
            )
            assert image.status_code == 200
            assert image.headers["content-type"] == "image/jpeg"
            pixel = PilImage.open(io.BytesIO(image.content)).convert("RGB").getpixel((5, 5))
            assert abs(pixel[0] - level) < 8, "left first, right second"  # type: ignore[index]
        # The cost accounting knows it was two images of a pair.
        request = h.store.requests[-1]
        assert request.detail is not None
        assert "images=2" in request.detail
        assert "stereo=true" in request.detail


async def test_vision_frames_too_far_apart_are_two_plain_images() -> None:
    async with harness() as h:
        late = _frame("right", "Front right", AT + timedelta(seconds=2))
        _, proposal = await _request(h, [LEFT, late])
        assert proposal["stereo"] is False
        assert proposal["image_count"] == 2
        assert h.factory.calls[-1][2].stereo is False


async def test_vision_stereo_preference_off_keeps_the_left_frame_only() -> None:
    async with harness() as h:
        owner = h.owner()
        defaults = await h.client.get(f"{API}/me/preferences", headers=owner.headers)
        assert defaults.status_code == 200
        assert defaults.json() == {
            "stereo_frames": True,
            "updated_at": None,
            "auto_analysis": False,
            "auto_analysis_available": True,
            "auto_cooldown_minutes": 10,
            "auto_daily_per_mower": 20,
        }
        saved = await h.client.put(
            f"{API}/me/preferences", json={"stereo_frames": False}, headers=owner.headers
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["stereo_frames"] is False
        again = await h.client.get(f"{API}/me/preferences", headers=owner.headers)
        assert again.json()["stereo_frames"] is False
        assert again.json()["updated_at"] is not None
        mower = await h.rig(owner)
        response = await h.client.post(
            f"{API}/mowers/{mower.mower_id}/recover",
            json={"task": "recover", "images": [_wire(RIGHT), _wire(LEFT)]},
            headers=owner.headers,
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["stereo"] is False
        assert body["image_count"] == 1
        assert [f["side"] for f in body["frames"]] == ["left"]


async def test_vision_single_frame_requests_keep_working_and_dark_photos_still_get_light() -> None:
    async with harness() as h:
        _, single = await _request(h, [Image(_jpeg(150), "image/jpeg")])
        assert single["stereo"] is False
        assert single["frames"] == [
            {"index": 0, "source": "upload", "camera": None, "side": None, "captured_at": None}
        ]
    async with harness() as h:
        # No image at all: the service's own photo, with the light at night (unchanged).
        _, own = await _request(h, [], night=True)
        assert own["stereo"] is False
        assert own["light_used"] is True
        assert [f["source"] for f in own["frames"]] == ["mower"]


async def test_vision_image_metadata_is_validated() -> None:
    async with harness() as h:
        owner = h.owner()
        mower = await h.rig(owner)
        bad = [
            {**_wire(LEFT), "side": "middle"},
            {**_wire(LEFT), "captured_at": "2026-10-11T12:00:00"},  # no time zone
            {**_wire(LEFT), "camera": "x" * 65},
        ]
        for image in bad:
            response = await h.client.post(
                f"{API}/mowers/{mower.mower_id}/recover",
                json={"task": "recover", "images": [image]},
                headers=owner.headers,
            )
            assert response.status_code == 422, image


async def test_vision_memory_store_keeps_and_forgets_preferences() -> None:
    store = MemoryStore()
    owner = (uuid4(), uuid4())
    assert await store.get_preferences(*owner) is None
    await store.put_preferences(UserPreferences(*owner, stereo_frames=False, updated_at=AT))
    found = await store.get_preferences(*owner)
    assert found is not None
    assert found.stereo_frames is False
    await store.delete_user(owner[0])
    assert await store.get_preferences(*owner) is None
