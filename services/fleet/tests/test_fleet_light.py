"""``set_light``: relation ``can_control``, plan features ``control`` and ``camera``, audit."""

from fleet_support import API, fake_gateway, harness
from mowdeck_contracts.v1 import SetLight
from mowdeck_service_kit.testing import FakeEntitlements

LIGHT_ON = {"command": {"kind": "set_light", "on": True}}


async def test_fleet_set_light_is_published_and_recorded() -> None:
    async with harness() as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        mower_id = (await h.register(owner))["id"]
        response = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=owner.headers, json=LIGHT_ON
        )
        assert response.status_code == 202, response.text
        assert response.json()["kind"] == "set_light"
        published = h.commands_published()
        stored = await h.store.tables.commands.get(command_id=published[0].command_id)
    assert published[0].command == SetLight(on=True)
    assert stored is not None
    assert stored.command == {"kind": "set_light", "on": True}


async def test_fleet_set_light_needs_the_camera_feature() -> None:
    plans = FakeEntitlements(plan="home", features=["view", "control", "emergency_stop"])
    async with harness(entitlements=plans) as h:
        owner = await h.owner()
        mower_id = (await h.register(owner))["id"]
        refused = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=owner.headers, json=LIGHT_ON
        )
        assert refused.status_code == 403
        assert refused.json()["errors"][0]["parameter"] == "camera"
