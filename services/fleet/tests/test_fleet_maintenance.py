"""Maintenance commands (ADR 0048): the right per kind, the plan feature ``control``, one mower
only, and the Fleet command record as their audit."""

from typing import Any

import pytest
from fleet_support import API, fake_gateway, harness
from mowdeck_service_kit.testing import FakeEntitlements

from fleet.domain.rules import (
    CAN_CONTROL,
    CAN_MANAGE_MOWERS,
    SINGLE_MOWER_KINDS,
    command_features,
    relation_for,
)

MANAGE_KINDS = ("restart_mower", "sync_map", "relocate_dock", "resync_rtk")


def _body(kind: str) -> dict[str, Any]:
    return {"command": {"kind": kind}}


def test_fleet_maintenance_relations_features_and_single_mower() -> None:
    for kind in MANAGE_KINDS:
        assert relation_for(kind) == CAN_MANAGE_MOWERS
    assert relation_for("leave_dock") == CAN_CONTROL
    for kind in (*MANAGE_KINDS, "leave_dock"):
        assert command_features(kind) == {"control"}
        assert kind in SINGLE_MOWER_KINDS


@pytest.mark.parametrize("kind", MANAGE_KINDS)
async def test_fleet_device_maintenance_needs_manage_rights_and_control(kind: str) -> None:
    plans = FakeEntitlements(plan="home", features=["view", "emergency_stop"])
    async with harness(entitlements=plans) as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        operator = await h.member(owner, "operator")  # controls and drives, does not manage
        manager = await h.member(owner, "manager")  # fleet.manage
        mower_id = (await h.register(owner))["id"]
        url = f"{API}/mowers/{mower_id}/commands"
        no_feature = await h.client.post(url, headers=manager.headers, json=_body(kind))
        plans.features.append("control")
        no_right = await h.client.post(url, headers=operator.headers, json=_body(kind))
        accepted = await h.client.post(url, headers=manager.headers, json=_body(kind))
        record = await h.client.get(accepted.headers["location"], headers=manager.headers)
        group_id = (await h.create(owner, "groups", name="North"))["id"]
        bulk = await h.client.post(
            f"{API}/groups/{group_id}/commands", headers=owner.headers, json=_body(kind)
        )
        published = h.commands_published()
    assert no_feature.status_code == 403
    assert no_feature.json()["errors"][0]["parameter"] == "control"
    assert no_right.status_code == 403
    assert accepted.status_code == 202, accepted.text
    assert record.status_code == 200, record.text
    assert record.json()["kind"] == kind  # the command record is the audit
    assert bulk.status_code == 422 and "one mower" in bulk.json()["detail"]
    assert [c.command.kind for c in published] == [kind]


async def test_fleet_leave_dock_needs_control_rights() -> None:
    async with harness() as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        operator = await h.member(owner, "operator")
        mower_id = (await h.register(owner))["id"]
        url = f"{API}/mowers/{mower_id}/commands"
        refused = await h.client.post(url, headers=viewer.headers, json=_body("leave_dock"))
        accepted = await h.client.post(url, headers=operator.headers, json=_body("leave_dock"))
        published = h.commands_published()
    assert refused.status_code == 403
    assert accepted.status_code == 202, accepted.text
    assert [c.command.kind for c in published] == ["leave_dock"]
