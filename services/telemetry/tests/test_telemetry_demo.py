"""Public demo (ADR 0068): the seed job's past mowing days and the response label."""

from typing import Any
from uuid import uuid4

from mowdeck_service_kit.demo import DEMO_HEADER, DEMO_SEED_HEADER
from telemetry_support import API, harness

SEED = "demo-seed-secret"
DEMO: dict[str, Any] = {"demo_mode": True, "demo_seed_token": SEED}


async def test_the_seed_job_ingests_past_mowing_days() -> None:
    async with harness(**DEMO) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        body = {
            "mowers": [
                {
                    "mower_id": str(mower_id),
                    "account_id": str(owner.account_id),
                    "connection_options": {"garden": "family"},
                }
            ],
            "days": 2,
        }
        response = await h.client.post(
            f"{API}/demo/history", json=body, headers={DEMO_SEED_HEADER: SEED}
        )
        assert response.status_code == 200, response.text
        assert response.headers[DEMO_HEADER] == "1"
        result = response.json()
        data = h.store.data
        positions = [p for (m, _), p in data.positions.items() if m == mower_id]
        days = {d for (m, d) in data.daily if m == mower_id}
        health = data.health.get(mower_id)
        latest = await h.client.get(f"{API}/mowers/{mower_id}/state", headers=owner.headers)
    assert result["states"] > 100
    assert result["stored"] == result["states"]
    assert len(positions) > 50
    assert len(days) == 2
    assert health is not None
    assert latest.status_code == 200, latest.text


async def test_only_the_seed_job_may_call_it() -> None:
    body = {"mowers": [{"mower_id": str(uuid4()), "account_id": str(uuid4())}]}
    async with harness(**DEMO) as h:
        without = await h.client.post(f"{API}/demo/history", json=body)
    async with harness(demo_seed_token=SEED) as h:
        outside = await h.client.post(
            f"{API}/demo/history", json=body, headers={DEMO_SEED_HEADER: SEED}
        )
    assert without.status_code == 403
    assert outside.status_code == 404
