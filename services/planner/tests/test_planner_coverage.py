"""``POST /coverage/gaps``: coverage from Telemetry's position track and the re-mow step."""

import itertools
import math
from datetime import timedelta
from typing import Any

from mowdeck_control import default_map, to_wgs84
from planner_support import API, T0, harness


def _track(points: list[tuple[float, float]], step: float = 0.2) -> list[tuple[float, float]]:
    """``(lat, lon)`` every ``step`` metres along a local polyline, like a position log."""
    reference = default_map().reference
    out: list[tuple[float, float]] = []
    for a, b in itertools.pairwise(points):
        count = max(1, math.ceil(math.dist(a, b) / step))
        for i in range(count):
            x = a[0] + (b[0] - a[0]) * i / count
            y = a[1] + (b[1] - a[1]) * i / count
            out.append(to_wgs84(reference, x, y))
    return out


def _window(**extra: Any) -> dict[str, Any]:
    return {
        "start": (T0 - timedelta(hours=1)).isoformat(),
        "end": T0.isoformat(),
        **extra,
    }


async def test_planner_gaps_after_a_full_run_are_small() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.mower(owner)
        plan = await h.client.post(
            f"{API}/plans",
            headers=owner.headers,
            json={
                "mower_id": str(mower_id),
                "area": {"area_id": "front"},
                "chain": [{"pattern": "stripes"}],
            },
        )
        path = [
            (float(p[0]), float(p[1]))
            for f in plan.json()["preview"]["features"]
            for p in f["geometry"]["coordinates"]
        ]
        h.track = _track(path)
        response = await h.client.post(
            f"{API}/coverage/gaps", headers=owner.headers, json=_window(mower_id=str(mower_id))
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["area_id"] == "front"  # the area with the most track points
    assert body["coverage_ratio"] > 0.95
    assert body["gap_area_m2"] < 0.05 * body["area_m2"]
    assert h.track_requests[0].mower_id == mower_id
    assert h.track_requests[0].account_id == owner.account_id


async def test_planner_gaps_of_half_a_run_give_a_remow_step() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.mower(owner)
        h.track = _track([(0.5, 0.5), (11.5, 0.5), (11.5, 1.0), (0.5, 1.0)])
        response = await h.client.post(
            f"{API}/coverage/gaps",
            headers=owner.headers,
            json=_window(mower_id=str(mower_id), area={"area_id": "front"}, min_gap_m2=0.5),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        remow = await h.preview(
            owner,
            mower_id=str(mower_id),
            area={"area_id": "front"},
            chain=[body["remow"]],
        )
    assert body["coverage_ratio"] < 0.2
    assert body["gaps"] and len(body["gaps"]) == len(body["gaps_wgs84"])
    assert body["remow"]["pattern"] == "remow"
    assert remow.status_code == 200, remow.text
    assert remow.json()["pattern_chain"] == ["remow"]


async def test_planner_gaps_errors() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.mower(owner)
        url = f"{API}/coverage/gaps"
        reversed_range = await h.client.post(
            url,
            headers=owner.headers,
            json={"mower_id": str(mower_id), "start": T0.isoformat(), "end": T0.isoformat()},
        )
        too_long = await h.client.post(
            url,
            headers=owner.headers,
            json={
                "mower_id": str(mower_id),
                "start": (T0 - timedelta(days=5)).isoformat(),
                "end": T0.isoformat(),
            },
        )
        h.track_error = "the time range may span at most 31 days"
        refused = await h.client.post(
            url, headers=owner.headers, json=_window(mower_id=str(mower_id))
        )
    assert reversed_range.status_code == 422
    assert too_long.status_code == 422
    assert refused.status_code == 422 and "telemetry refused" in refused.json()["detail"]


async def test_planner_gaps_without_telemetry_is_unavailable() -> None:
    async with harness(telemetry=False) as h:
        owner = await h.owner()
        mower_id = await h.mower(owner)
        response = await h.client.post(
            f"{API}/coverage/gaps", headers=owner.headers, json=_window(mower_id=str(mower_id))
        )
    assert response.status_code == 503
    assert "telemetry" in response.json()["detail"]
