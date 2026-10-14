"""Panorama calibration (ADR 0037): ``POST /calibration/correspondences`` returns corresponding
points of a left and a right frame, validated, with cost accounting (``kind = calibration``) and
no proposal; the fake provider and the LiteLLM provider answer the new port method."""

import base64
import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

from mowdeck_control import flat_png
from mowdeck_service_kit.testing import FakeEntitlements
from test_vision_llm import Scripted, _module, _provider, _response
from vision_support import API, T0, StaticFactory, harness

from vision.adapters.providers.calibration_prompt import CORRESPONDENCE_SCHEMA
from vision.adapters.providers.fake import FakeVisionProvider
from vision.domain.calibration import (
    CorrespondencesOut,
    FrameSize,
    PointPair,
    image_size,
    usable_points,
)
from vision.domain.models import Image

URL = f"{API}/calibration/correspondences"


def _frame(side: str, width: int = 640, height: int = 480, **extra: Any) -> dict[str, Any]:
    data = base64.b64encode(flat_png(width, height, (90, 120, 60))).decode()
    return {"media_type": "image/png", "data": data, "side": side, **extra}


def _body(mower_id: Any, **extra: Any) -> dict[str, Any]:
    return {"mower_id": str(mower_id), "left": _frame("left"), "right": _frame("right"), **extra}


def _shifted(count: int = 9) -> CorrespondencesOut:
    """Points of a 560 px shift plus one off the left image."""
    points = [
        PointPair(left=[560.0 + 8 * i, 40.0 + 40 * i], right=[8.0 * i, 40.0 + 40 * i], label="x")
        for i in range(count)
    ]
    points.append(PointPair(left=[700.0, 10.0], right=[5.0, 5.0], label="off the image"))
    return CorrespondencesOut(points=points)


async def test_vision_calibration_returns_validated_points_and_records_cost() -> None:
    factory = StaticFactory(correspondences=_shifted())
    async with harness(factory=factory) as h:
        owner = h.owner()
        mower_id = h.mower(owner)
        response = await h.client.post(URL, json=_body(mower_id), headers=owner.headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["points"]) == 9 and body["dropped"] == 1
        assert body["points"][0] == {"left": [560.0, 40.0], "right": [0.0, 40.0], "label": "x"}
        assert body["left_size"] == {"width": 640, "height": 480}
        assert body["model"] == "fake/recover"
        assert body["cost"]["cost_usd"] == 0.0072 and body["cost"]["hosted"] is True
        [call] = factory.correspondence_calls
        assert call[1] == FrameSize(640, 480)
        [request] = h.store.requests
        assert request.kind == "calibration" and request.outcome == "ok"
        assert request.proposal_id is None and request.cost_usd == 0.0072
        assert request.detail is not None and "points=9 dropped=1" in request.detail
        # No proposal: calibration never drives.
        assert await h.store.open_proposal(mower_id) is None


async def test_vision_calibration_needs_view_on_the_mower() -> None:
    async with harness(factory=StaticFactory(correspondences=_shifted())) as h:
        owner = h.owner()
        mower_id = h.mower(owner)
        viewer = h.viewer(owner, mower_id)
        ok = await h.client.post(URL, json=_body(mower_id), headers=viewer.headers)
        assert ok.status_code == 200, ok.text
        stranger = h.owner()
        hidden = await h.client.post(URL, json=_body(mower_id), headers=stranger.headers)
        assert hidden.status_code == 404
        unknown = await h.client.post(URL, json=_body(uuid4()), headers=owner.headers)
        assert unknown.status_code == 404
        anonymous = await h.client.post(URL, json=_body(mower_id))
        assert anonymous.status_code == 401


async def test_vision_calibration_needs_the_plan_feature() -> None:
    plans = FakeEntitlements(plan="home", features=["view", "control"])
    async with harness(entitlements=plans, factory=StaticFactory(correspondences=_shifted())) as h:
        owner = h.owner()
        response = await h.client.post(URL, json=_body(h.mower(owner)), headers=owner.headers)
        assert response.status_code == 403
        assert response.json()["type"].endswith("feature-not-in-plan")


async def test_vision_calibration_without_a_provider_is_a_conflict() -> None:
    factory = StaticFactory(correspondences=_shifted())
    async with harness(factory=factory, vision_default_enabled=False) as h:
        owner = h.owner()
        response = await h.client.post(URL, json=_body(h.mower(owner)), headers=owner.headers)
        assert response.status_code == 409
        assert response.json()["type"].endswith("vision-provider-missing")


async def test_vision_calibration_model_failures_are_502_and_recorded() -> None:
    async with harness(factory=StaticFactory(fail="the model is down")) as h:
        owner = h.owner()
        response = await h.client.post(URL, json=_body(h.mower(owner)), headers=owner.headers)
        assert response.status_code == 502
        assert response.json()["type"].endswith("vision-provider-failed")
        [request] = h.store.requests
        assert request.kind == "calibration" and request.outcome == "error"
    async with harness(factory=StaticFactory(correspondences=_shifted(3))) as h:
        owner = h.owner()
        response = await h.client.post(URL, json=_body(h.mower(owner)), headers=owner.headers)
        assert response.status_code == 502
        assert "only 3 usable" in response.json()["detail"]
        assert h.store.requests[0].outcome == "too_few_points"


async def test_vision_calibration_has_a_total_time_budget() -> None:
    """A model that never finishes within the budget is cut off with 504 and recorded; a model
    that answers in time is unaffected by the budget."""
    slow = StaticFactory(correspondences=_shifted(), delay=0.5)
    async with harness(factory=slow, vision_calibration_timeout_seconds=0.05) as h:
        owner = h.owner()
        response = await h.client.post(URL, json=_body(h.mower(owner)), headers=owner.headers)
        assert response.status_code == 504, response.text
        body = response.json()
        assert body["type"].endswith("vision-provider-timeout")
        assert "did not answer within 0 s" in body["detail"]
        [request] = h.store.requests
        assert request.kind == "calibration" and request.outcome == "timeout"
        assert request.detail == "time budget"
    quick = StaticFactory(correspondences=_shifted(), delay=0.01)
    async with harness(factory=quick, vision_calibration_timeout_seconds=1.0) as h:
        owner = h.owner()
        response = await h.client.post(URL, json=_body(h.mower(owner)), headers=owner.headers)
        assert response.status_code == 200, response.text


async def test_vision_calibration_has_its_own_daily_cap() -> None:
    factory = StaticFactory(correspondences=_shifted())
    async with harness(factory=factory, vision_calibration_daily_requests=1) as h:
        owner = h.owner()
        mower_id = h.mower(owner)
        first = await h.client.post(URL, json=_body(mower_id), headers=owner.headers)
        assert first.status_code == 200
        second = await h.client.post(URL, json=_body(mower_id), headers=owner.headers)
        assert second.status_code == 429
        assert second.json()["type"].endswith("vision-quota")


async def test_vision_calibration_refuses_unusable_frames() -> None:
    async with harness(factory=StaticFactory(correspondences=_shifted())) as h:
        owner = h.owner()
        mower_id = h.mower(owner)
        broken = _body(mower_id)
        broken["left"]["data"] = base64.b64encode(b"not an image").decode()
        response = await h.client.post(URL, json=broken, headers=owner.headers)
        assert response.status_code == 422
        apart = _body(
            mower_id,
            left=_frame("left", captured_at=T0.isoformat()),
            right=_frame("right", captured_at=(T0 + timedelta(seconds=2)).isoformat()),
        )
        response = await h.client.post(URL, json=apart, headers=owner.headers)
        assert response.status_code == 422
        assert "apart" in response.json()["detail"]
        assert h.store.requests == []


def test_vision_calibration_usable_points_drop_off_image_and_duplicates() -> None:
    size = FrameSize(100, 50)
    points = [
        PointPair(left=[10, 10], right=[20, 20]),
        PointPair(left=[10, 10], right=[20, 20]),
        PointPair(left=[101, 10], right=[20, 20]),
        PointPair(left=[10, 10], right=[20, -3]),
        PointPair(left=[100.4, 50.2], right=[0, 0]),
    ]
    kept = usable_points(points, size, size)
    assert [(p.left, p.right) for p in kept] == [([10, 10], [20, 20]), ([100.4, 50.2], [0, 0])]
    assert image_size(flat_png(32, 24, (1, 2, 3))) == FrameSize(32, 24)
    assert image_size(b"nope") is None
    # Long answers are cut to 16 points, long labels to 60 characters.
    many = CorrespondencesOut.model_validate(
        {"points": [{"left": [1, 1], "right": [1, 1], "label": "y" * 99}] * 20}
    )
    assert len(many.points) == 16 and len(many.points[0].label) == 60


async def test_vision_calibration_fake_provider_answers_a_shift_with_one_outlier() -> None:
    image = Image(flat_png(8, 6, (1, 2, 3)), "image/png")
    size = FrameSize(640, 480)
    answer = await FakeVisionProvider("fake/recover").correspond(image, image, size, size)
    points = answer.result.points
    assert len(points) == 10 and answer.usage.cost_usd == 0.0
    shifts = {round(p.left[0] - p.right[0], 1) for p in points[:-1]}
    assert shifts == {round(0.88 * 640, 1)}
    assert all(p.left[1] == p.right[1] for p in points[:-1])
    assert points[-1].label == "wrong on purpose"
    assert usable_points(points, size, size) == points


async def test_vision_calibration_llm_asks_with_the_schema_and_both_images() -> None:
    answer = {"points": [{"left": [600, 100], "right": [40, 100], "label": "post"}] * 8}
    completion = Scripted(_response(json.dumps(answer), prompt=2000, completion=200))
    image = Image(flat_png(16, 12, (74, 140, 66)), "image/png")
    result = await _provider(completion, _module(cost=0.004)).correspond(
        image, image, FrameSize(640, 480), FrameSize(640, 480)
    )
    assert len(result.result.points) == 8 and result.result.points[0].label == "post"
    assert result.usage.cost_usd == 0.004 and result.usage.input_tokens == 2000
    [call] = completion.calls
    assert call["response_format"]["json_schema"]["schema"] == CORRESPONDENCE_SCHEMA
    system, user = call["messages"]
    assert "8 and 16" in system["content"]
    images = [part for part in user["content"] if part["type"] == "image_url"]
    assert len(images) == 2
    assert "640 x 480" in user["content"][0]["text"]
