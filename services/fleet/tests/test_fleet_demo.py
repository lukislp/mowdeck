"""Public demo (ADR 0068): only simulated mowers, no manufacturer accounts, the demo mowers
stay as the seed job made them; commands and the rest work."""

from typing import Any

from fleet_support import API, harness, make_settings
from mowdeck_service_kit.demo import DEMO_HEADER, DEMO_SEED_HEADER

SEED = "demo-seed-secret"
DEMO: dict[str, Any] = {
    "demo_mode": True,
    "demo_seed_token": SEED,
    "allowed_connection_kinds": "simulated,cloud,ble_bridge",
}
DEMO_PROBLEM = "urn:mowdeck:problem:demo-mode"


def test_demo_mode_offers_only_simulated_mowers() -> None:
    assert make_settings(**DEMO).connection_kinds == frozenset({"simulated"})
    assert "cloud" in make_settings(allowed_connection_kinds="simulated,cloud").connection_kinds


async def test_the_seed_job_adds_mowers_and_visitors_cannot() -> None:
    async with harness(**DEMO) as h:
        owner = await h.owner()
        body = {"name": "Demo", "connection_kind": "simulated"}
        visitor = await h.client.post(f"{API}/mowers", headers=owner.headers, json=body)
        seeded = await h.client.post(
            f"{API}/mowers", headers={**owner.headers, DEMO_SEED_HEADER: SEED}, json=body
        )
        cloud = await h.client.post(
            f"{API}/mowers",
            headers={**owner.headers, DEMO_SEED_HEADER: SEED},
            json={"name": "Real", "connection_kind": "cloud"},
        )
        kinds = await h.client.get(f"{API}/connection-kinds", headers=owner.headers)
    assert visitor.status_code == 403
    assert visitor.json()["type"] == DEMO_PROBLEM
    assert seeded.status_code == 201, seeded.text
    assert seeded.headers[DEMO_HEADER] == "1"
    assert cloud.status_code in (400, 422), cloud.text  # not offered, even to the seed job
    assert [k["kind"] for k in kinds.json()["items"]] == ["simulated"]


async def test_visitors_drive_but_do_not_reconfigure_the_demo_mowers() -> None:
    async with harness(**DEMO) as h:
        owner = await h.owner()
        mower = (
            await h.client.post(
                f"{API}/mowers",
                headers={**owner.headers, DEMO_SEED_HEADER: SEED},
                json={"name": "Demo", "connection_kind": "simulated"},
            )
        ).json()
        base = f"{API}/mowers/{mower['id']}"
        rename = await h.client.patch(base, headers=owner.headers, json={"name": "Mine"})
        delete = await h.client.delete(base, headers=owner.headers)
        bridge = await h.client.put(
            f"{base}/links/ble_bridge", headers=owner.headers, json={"host": "10.0.0.5"}
        )
        link = await h.client.post(
            f"{API}/linked-accounts",
            headers=owner.headers,
            json={"vendor": "mammotion", "username": "me@example.org", "password": "x" * 12},
        )
        command = await h.client.post(
            f"{base}/commands", headers=owner.headers, json={"command": {"kind": "pause_mow"}}
        )
    for refused in (rename, delete, bridge, link):
        assert refused.status_code == 403, refused.text
        assert refused.json()["type"] == DEMO_PROBLEM
    assert command.status_code == 202, command.text


async def test_no_cloud_provider_is_offered_or_linked_in_the_demo() -> None:
    """ADR 0066: the demo lists no provider, and linking any provider and a
    re-authentication are refused before a gateway is asked."""
    async with harness(**DEMO) as h:
        owner = await h.owner()
        base = f"{API}/linked-accounts"
        body = {"provider": "acme", "credentials": {"email": "a@x.test", "password": "secret"}}
        listed = await h.client.get(f"{base}/providers", headers=owner.headers)
        refused = [
            await h.client.post(base, headers=owner.headers, json=body),
            await h.client.post(
                f"{base}/00000000-0000-0000-0000-000000000001/sync",
                headers=owner.headers,
                json={"credentials": {"password": "new"}},
            ),
        ]
    assert listed.status_code == 200 and listed.json() == {"items": []}
    for response in refused:
        assert response.status_code == 403, response.text
        assert response.json()["type"] == DEMO_PROBLEM
        assert "secret" not in response.text
