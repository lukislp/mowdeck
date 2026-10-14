"""Dark photos: the light goes on for a second photo and off again after the analysis."""

import io
from typing import Any

from mowdeck_contracts.v1 import IssueCommandErrorCode, MowerActivity, SetLight
from mowdeck_control import flat_png
from PIL import Image as PilImage
from vision_support import API, Harness, SimRig, harness, wait_for

from vision.domain.frames import mean_luminance, too_dark
from vision.domain.models import ProposalState


def _jpeg(level: int) -> bytes:
    buffer = io.BytesIO()
    PilImage.new("RGB", (64, 48), (level, level, level)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_vision_mean_luminance_of_dark_and_bright_frames() -> None:
    dark, bright = _jpeg(12), _jpeg(200)
    assert mean_luminance(dark) is not None and mean_luminance(dark) < 20  # type: ignore[operator]
    assert mean_luminance(bright) is not None and mean_luminance(bright) > 180  # type: ignore[operator]
    assert mean_luminance(flat_png(4, 3, (255, 255, 255))) == 255.0
    assert mean_luminance(b"not an image") is None
    assert too_dark(dark, 40) and not too_dark(bright, 40)
    assert not too_dark(dark, 0), "a threshold of 0 disables the check"
    assert not too_dark(b"not an image", 40)


def _lights(h: Harness) -> list[bool]:
    return [r.command.command.on for r in h.fleet.issued if isinstance(r.command.command, SetLight)]


async def _assist(h: Harness, **rig_config: Any) -> tuple[dict[str, Any], SimRig]:
    owner = h.owner()
    rig = await h.rig(owner, **rig_config)
    rig.sim.inject_stuck()
    await wait_for(
        lambda: (
            (o := h.service.view.state(rig.mower_id)) is not None
            and o.state.activity is MowerActivity.ERROR
        )
    )
    await wait_for(lambda: h.service.view.capabilities(rig.mower_id) is not None)
    response = await h.client.post(
        f"{API}/mowers/{rig.mower_id}/recover", json={"task": "recover"}, headers=owner.headers
    )
    assert response.status_code == 202, response.text
    created = response.json()
    done = await h.until(created["id"], owner, ProposalState.PROPOSED)
    await h.service.idle()
    image = await h.client.get(f"{API}/proposals/{created['id']}/images/0", headers=owner.headers)
    assert image.status_code == 200
    done["luminance"] = mean_luminance(image.content)
    return done, rig


async def test_vision_dark_photo_is_retaken_with_the_light_on() -> None:
    async with harness() as h:
        proposal, rig = await _assist(h, night=True)
        assert proposal["light_used"] is True
        assert proposal["luminance"] > 40  # the model saw the lit frame
        assert _lights(h) == [True, False], "on for the photo, off after the analysis"
        light = [r for r in h.fleet.issued if isinstance(r.command.command, SetLight)]
        assert {r.origin for r in light} == {"vision"}
        await wait_for(lambda: rig.sim.snapshot().light_on is False)


async def test_vision_bright_photo_leaves_the_light_alone() -> None:
    async with harness() as h:
        proposal, _ = await _assist(h)
        assert proposal["light_used"] is False
        assert proposal["luminance"] > 40
        assert _lights(h) == []


async def test_vision_dark_photo_without_a_light_goes_on_dark() -> None:
    async with harness() as h:
        proposal, _ = await _assist(h, night=True, light=False)
        assert proposal["light_used"] is False
        assert proposal["luminance"] < 40
        assert _lights(h) == []


async def test_vision_refused_light_does_not_break_the_analysis() -> None:
    async with harness() as h:
        h.fleet.refuse_issue = IssueCommandErrorCode.FORBIDDEN
        proposal, _ = await _assist(h, night=True)
        assert proposal["light_used"] is False
        assert proposal["luminance"] < 40
        assert _lights(h) == [True], "tried once, nothing to switch off"


async def test_vision_light_already_on_is_not_switched() -> None:
    async with harness() as h:
        owner = h.owner()
        rig = await h.rig(owner, night=True)
        await rig.sim.execute(SetLight(on=True))
        await wait_for(
            lambda: (
                (o := h.service.view.state(rig.mower_id)) is not None and o.state.light_on is True
            )
        )
        response = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover",
            json={"task": "find_dock"},
            headers=owner.headers,
        )
        assert response.status_code == 202, response.text
        assert response.json()["light_used"] is False
        await h.service.idle()
        assert _lights(h) == []
