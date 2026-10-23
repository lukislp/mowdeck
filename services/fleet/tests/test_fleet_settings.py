"""``set_setting`` (ADR 0046): relation ``can_manage_mowers``, plan feature ``control``, audit."""

from fleet_support import API, fake_gateway, harness
from mowdeck_contracts.v1 import SetSetting, TimeWindow
from mowdeck_service_kit.testing import FakeEntitlements

QUIET = {
    "command": {
        "kind": "set_setting",
        "key": "non_work_hours",
        "value": {"start": "22:00", "end": "06:00"},
    }
}


async def test_fleet_set_setting_is_published_and_recorded() -> None:
    async with harness() as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        mower_id = (await h.register(owner))["id"]
        response = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=owner.headers, json=QUIET
        )
        assert response.status_code == 202, response.text
        assert response.json()["kind"] == "set_setting"
        published = h.commands_published()
        stored = await h.store.tables.commands.get(command_id=published[0].command_id)
    assert published[0].command == SetSetting(
        key="non_work_hours", value=TimeWindow(start="22:00", end="06:00")
    )
    assert stored is not None
    assert stored.kind == "set_setting"
    assert stored.command == QUIET["command"]


async def test_fleet_set_setting_needs_manage_rights_and_the_control_feature() -> None:
    plans = FakeEntitlements(plan="home", features=["view", "emergency_stop"])
    async with harness(entitlements=plans) as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        operator = await h.member(owner, "operator")  # controls and drives, does not manage
        mower_id = (await h.register(owner))["id"]
        url = f"{API}/mowers/{mower_id}/commands"
        body = {"command": {"kind": "set_setting", "key": "rain_detection", "value": True}}
        no_feature = await h.client.post(url, headers=owner.headers, json=body)
        plans.features.append("control")
        no_right = await h.client.post(url, headers=operator.headers, json=body)
        accepted = await h.client.post(url, headers=owner.headers, json=body)
        published = h.commands_published()
    assert no_feature.status_code == 403
    assert no_feature.json()["errors"][0]["parameter"] == "control"
    assert no_right.status_code == 403
    assert accepted.status_code == 202, accepted.text
    assert [c.command for c in published] == [SetSetting(key="rain_detection", value=True)]


async def test_fleet_set_setting_refuses_a_malformed_key() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = (await h.register(owner))["id"]
        body = {"command": {"kind": "set_setting", "key": "Rain Detection", "value": True}}
        response = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=owner.headers, json=body
        )
    assert response.status_code == 422
