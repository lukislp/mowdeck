"""Drive calibration (ADR 0042): the gates of ``drive_calibrate`` and ``set_drive_speed``, the
managed calibration options of the drive link, and ``PUT``/``DELETE
/mowers/{id}/drive-calibration`` on a mower with a bridge link, on a simulated mower
(connection options) and on a mower with neither."""

from typing import Any
from uuid import UUID

import pytest
from fleet_support import API, fake_gateway, harness
from mowdeck_contracts.v1 import DRIVE_CALIBRATION_OPTION_KEYS
from mowdeck_service_kit.testing import FakeEntitlements
from test_fleet_links import LINK, _cloud_mower, _gateway, _link_url, _settings, _updates

from fleet.adapters.api.deps import actor_of
from fleet.application.links import DRIVE_CALIBRATION_REMOVED, DRIVE_CALIBRATION_SET
from fleet.domain.connections import SCHEMAS
from fleet.domain.errors import InvalidInput
from fleet.domain.rules import validate_link_options

CALIBRATION: dict[str, Any] = {
    "full_speed_mps": 0.42,
    "full_yaw_rate_dps": 61.5,
    "dead_zone": 0.12,
    "turn_dead_zone": 0.08,
    "drive_speed_setting": 0.4,
    "calibrated_at": "2026-10-15T10:00:00Z",
}
OPTIONS: dict[str, Any] = {
    "drive_full_speed_mps": 0.42,
    "drive_full_yaw_rate_dps": 61.5,
    "drive_dead_zone": 0.12,
    "drive_turn_dead_zone": 0.08,
    "drive_speed_setting": 0.4,
    "calibrated_at": "2026-10-15T10:00:00+00:00",
}
CALIBRATE = {"command": {"kind": "drive_calibrate", "step_s": 6.0}}
SPEED = {"command": {"kind": "set_drive_speed", "speed_mps": 0.5}}


def _url(mower_id: str) -> str:
    return f"{API}/mowers/{mower_id}/drive-calibration"


# --- command gates -----------------------------------------------------------------------------


@pytest.mark.parametrize("body", [CALIBRATE, SPEED], ids=["drive_calibrate", "set_drive_speed"])
async def test_fleet_calibration_commands_need_drive_rights_control_and_one_mower(
    body: dict[str, Any],
) -> None:
    plans = FakeEntitlements(plan="home", features=["view", "emergency_stop"])
    async with harness(entitlements=plans) as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        operator = await h.member(owner, "operator")
        manager = await h.member(owner, "manager")  # no mower.drive
        mower_id = (await h.register(owner))["id"]
        url = f"{API}/mowers/{mower_id}/commands"
        no_feature = await h.client.post(url, headers=operator.headers, json=body)
        plans.features.append("control")  # no spot_mowing: calibration is not spot mowing
        no_drive = await h.client.post(url, headers=manager.headers, json=body)
        accepted = await h.client.post(url, headers=operator.headers, json=body)
        group_id = (await h.create(owner, "groups", name="North"))["id"]
        bulk = await h.client.post(
            f"{API}/groups/{group_id}/commands", headers=owner.headers, json=body
        )
        published = h.commands_published()
    assert no_feature.status_code == 403
    assert no_feature.json()["errors"][0]["parameter"] == "control"
    assert no_drive.status_code == 403
    assert accepted.status_code == 202, accepted.text
    assert bulk.status_code == 422 and "one mower" in bulk.json()["detail"]
    assert [c.command.kind for c in published] == [body["command"]["kind"]]


# --- the link schema ---------------------------------------------------------------------------


def test_fleet_bridge_schema_declares_the_calibration_as_managed_options() -> None:
    specs = {o.key: o for o in SCHEMAS["ble_bridge"].options}
    assert set(specs) >= DRIVE_CALIBRATION_OPTION_KEYS
    for key in DRIVE_CALIBRATION_OPTION_KEYS:
        assert specs[key].managed and specs[key].advanced and not specs[key].secret
    assert SCHEMAS["ble_bridge"].managed_keys >= DRIVE_CALIBRATION_OPTION_KEYS
    assert specs["calibrated_at"].type == "string"
    assert specs["drive_full_yaw_rate_dps"].maximum == 720
    public, secret = validate_link_options("ble_bridge", {**LINK, **OPTIONS}, max_bytes=4096)
    assert {k: public[k] for k in OPTIONS} == OPTIONS and set(secret) == {"noise_psk"}
    with pytest.raises(InvalidInput) as refused:
        validate_link_options("ble_bridge", {**LINK, "drive_dead_zone": 0.95}, max_bytes=4096)
    assert refused.value.fields[0].path == "links/ble_bridge/drive_dead_zone"


# --- bridge link -------------------------------------------------------------------------------


async def test_fleet_calibration_is_stored_on_the_bridge_link_and_kept_by_a_replace() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        mower_id = await _cloud_mower(h, owner)
        await h.client.put(_link_url(mower_id), headers=owner.headers, json=LINK)
        rev_before = _updates(h)[-1].links["ble_bridge"]["credentials_rev"]
        stored = await h.client.put(_url(mower_id), headers=owner.headers, json=CALIBRATION)
        mower = await h.client.get(f"{API}/mowers/{mower_id}", headers=owner.headers)
        published = _updates(h)[-1]
        # a client edits the bridge without knowing the calibration: it stays
        replaced = await h.client.put(
            _link_url(mower_id),
            headers=owner.headers,
            json={**LINK, "host": "bridge-2.local", "noise_psk": "***"},
        )
        kept = await h.client.get(f"{API}/mowers/{mower_id}", headers=owner.headers)
        removed = await h.client.delete(_url(mower_id), headers=owner.headers)
        after = await h.client.get(f"{API}/mowers/{mower_id}", headers=owner.headers)
        last = _updates(h)[-1]
        again = await h.client.delete(_url(mower_id), headers=owner.headers)
        audit = list(h.store.tables.audit.rows.values())
    assert stored.status_code == 200, stored.text
    assert stored.json() == {**CALIBRATION, "calibrated_at": "2026-10-15T10:00:00Z"}
    link = mower.json()["links"]["ble_bridge"]["options"]
    assert {k: link[k] for k in OPTIONS} == OPTIONS and link["noise_psk"] == "***"
    assert mower.json()["drive_calibration"] == stored.json()
    assert {k: published.links["ble_bridge"][k] for k in OPTIONS} == OPTIONS
    assert published.links["ble_bridge"]["credentials_rev"] == rev_before  # key untouched
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["options"]["host"] == "bridge-2.local"
    assert {k: replaced.json()["options"][k] for k in OPTIONS} == OPTIONS
    assert kept.json()["drive_calibration"] == stored.json()
    assert removed.status_code == 204 and again.status_code == 204
    assert after.json()["drive_calibration"] is None
    assert not DRIVE_CALIBRATION_OPTION_KEYS & set(after.json()["links"]["ble_bridge"]["options"])
    assert not DRIVE_CALIBRATION_OPTION_KEYS & set(last.links["ble_bridge"])
    sets = [a for a in audit if a.action == DRIVE_CALIBRATION_SET]
    removals = [a for a in audit if a.action == DRIVE_CALIBRATION_REMOVED]
    assert len(sets) == 1 and sets[0].details == {"stored_on": "ble_bridge", **OPTIONS}
    assert len(removals) == 1  # the second delete found nothing to remove
    assert all("noise_psk" not in str(a.details) for a in audit)


async def test_fleet_bridge_link_replace_refuses_calibration_keys_from_clients() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        mower_id = await _cloud_mower(h, owner)
        await h.client.put(_link_url(mower_id), headers=owner.headers, json=LINK)
        await h.client.put(_url(mower_id), headers=owner.headers, json=CALIBRATION)
        smuggled = await h.client.put(
            _link_url(mower_id),
            headers=owner.headers,
            json={**LINK, "noise_psk": "***", "drive_full_speed_mps": 3.0},
        )
        service = h.app.state.fleet.links
        link = await service.get(owner.account_id, UUID(mower_id), "ble_bridge")
    assert smuggled.status_code == 422  # the request body knows no calibration keys
    assert link.options["drive_full_speed_mps"] == 0.42


async def test_fleet_link_service_drops_incoming_calibration_keys() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        mower_id = UUID(await _cloud_mower(h, owner))
        service = h.app.state.fleet.links
        actor = actor_of(owner.principal)
        created = await service.set(
            actor, owner.account_id, mower_id, "ble_bridge", {**LINK, **OPTIONS}
        )
    assert not DRIVE_CALIBRATION_OPTION_KEYS & set(created.options)


# --- connection options and refusals -----------------------------------------------------------


async def test_fleet_calibration_of_a_simulated_mower_lives_in_its_connection_options() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = (await h.register(owner, connection_options={"time_scale": 2}))["id"]
        stored = await h.client.put(_url(mower_id), headers=owner.headers, json=CALIBRATION)
        mower = await h.client.get(f"{API}/mowers/{mower_id}", headers=owner.headers)
        patched = await h.client.patch(
            f"{API}/mowers/{mower_id}",
            headers=owner.headers,
            json={"connection_options": {"time_scale": 3, "drive_full_speed_mps": 4.0}},
        )
        updates = _updates(h)
        removed = await h.client.delete(_url(mower_id), headers=owner.headers)
        after = await h.client.get(f"{API}/mowers/{mower_id}", headers=owner.headers)
    assert stored.status_code == 200, stored.text
    options = mower.json()["connection_options"]
    assert options["time_scale"] == 2 and {k: options[k] for k in OPTIONS} == OPTIONS
    assert mower.json()["drive_calibration"] == stored.json()
    assert updates and updates[0].connection_options["drive_full_speed_mps"] == 0.42
    # a PATCH keeps the stored calibration and drops a smuggled value
    assert patched.status_code == 200, patched.text
    assert patched.json()["connection_options"]["time_scale"] == 3
    assert patched.json()["connection_options"]["drive_full_speed_mps"] == 0.42
    assert removed.status_code == 204
    assert after.json()["drive_calibration"] is None
    assert after.json()["connection_options"] == {"time_scale": 3}


async def test_fleet_calibration_lives_on_the_connection_and_needs_drive_rights() -> None:
    """A cloud mower without a bridge stores its calibration in its connection options: the
    cloud drives too (ADR 0049)."""
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        operator = await h.member(owner, "operator")
        manager = await h.member(owner, "manager")  # sees the mower, no mower.drive
        stranger = await h.owner()
        cloud = await _cloud_mower(h, owner)
        simulated = (await h.register(owner))["id"]
        on_cloud = await h.client.put(_url(cloud), headers=owner.headers, json=CALIBRATION)
        forbidden = await h.client.put(_url(simulated), headers=manager.headers, json=CALIBRATION)
        forbidden_delete = await h.client.delete(_url(simulated), headers=manager.headers)
        hidden = await h.client.put(_url(simulated), headers=stranger.headers, json=CALIBRATION)
        hidden_delete = await h.client.delete(_url(simulated), headers=stranger.headers)
        bad = await h.client.put(
            _url(simulated), headers=operator.headers, json={**CALIBRATION, "dead_zone": 0.95}
        )
        allowed = await h.client.put(_url(simulated), headers=operator.headers, json=CALIBRATION)
        listed = await h.client.get(f"{API}/mowers", headers=owner.headers)
    assert on_cloud.status_code == 200, on_cloud.text
    assert forbidden.status_code == 403 and forbidden_delete.status_code == 403
    assert hidden.status_code == 404 and hidden_delete.status_code == 404
    assert bad.status_code == 422
    assert allowed.status_code == 200, allowed.text
    by_id = {m["id"]: m for m in listed.json()["items"]}
    assert by_id[simulated]["drive_calibration"]["full_speed_mps"] == 0.42
    assert by_id[cloud]["drive_calibration"]["full_speed_mps"] == 0.42


# --- the manual-drive speed limit of the bridge link -------------------------------------------


async def test_fleet_bridge_link_takes_a_manual_drive_speed_limit() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        mower_id = await _cloud_mower(h, owner)
        stored = await h.client.put(
            _link_url(mower_id), headers=owner.headers, json={**LINK, "manual_drive_speed_mps": 0.6}
        )
        published = _updates(h)[-1]
        too_fast = await h.client.put(
            _link_url(mower_id),
            headers=owner.headers,
            json={**LINK, "noise_psk": "***", "manual_drive_speed_mps": 1.5},
        )
    assert stored.status_code == 200, stored.text
    assert stored.json()["options"]["manual_drive_speed_mps"] == 0.6
    assert published.links["ble_bridge"]["manual_drive_speed_mps"] == 0.6
    assert too_fast.status_code == 422
    spec = {o.key: o for o in SCHEMAS["ble_bridge"].options}["manual_drive_speed_mps"]
    assert (spec.minimum, spec.maximum, spec.managed) == (0.1, 1.0, False)
