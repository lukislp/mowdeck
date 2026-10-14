"""Plan enforcement in Fleet (ADR 0028): limits on mowers, sites and groups, the ``control``
feature on commands (never the emergency stop), bulk commands, and the bus client wiring."""

from uuid import UUID

from fleet_support import API, fake_gateway, harness
from mowdeck_contracts.v1 import (
    CommandRequest,
    EntitlementCheckRequest,
    EntitlementSnapshot,
    IssueCommandErrorCode,
    IssueCommandReply,
    IssueCommandRequest,
    PlanLimits,
    PlanStatus,
    PrincipalKind,
    PrincipalRef,
    StartMow,
)
from mowdeck_contracts.v1.subjects import ENTITLEMENTS_CHECK, FLEET_COMMANDS_ISSUE
from mowdeck_service_kit.bus import BusMessage
from mowdeck_service_kit.entitlements import EntitlementsClient
from mowdeck_service_kit.testing import FakeEntitlements

START = {"command": {"kind": "start_mow", "area_ids": ["front"]}}
STOP = {"command": {"kind": "emergency_stop"}}


def _home() -> FakeEntitlements:
    return FakeEntitlements(
        plan="home",
        features=["view", "control", "emergency_stop"],
        limits=PlanLimits(max_mowers=2, max_sites=1, max_groups=1),
    )


async def test_fleet_plan_limit_on_mowers_answers_409_plan_limit() -> None:
    plans = _home()
    async with harness(entitlements=plans) as h:
        owner = await h.owner()
        await h.register(owner)
        await h.register(owner)
        response = await h.client.post(
            f"{API}/mowers",
            headers=owner.headers,
            json={"name": "third", "connection_kind": "simulated"},
        )
        assert response.status_code == 409
        problem = response.json()
        assert problem["type"] == "urn:mowdeck:problem:plan-limit"
        assert {(e["parameter"], e["code"], e["detail"]) for e in problem["errors"]} == {
            ("max_mowers", "limit", "2"),
            ("max_mowers", "current", "2"),
        }
        listed = await h.client.get(f"{API}/mowers", headers=owner.headers)
        assert len(listed.json()["items"]) == 2  # nothing was written
    assert ("limit", "max_mowers", 2) in plans.checks


async def test_fleet_plan_limits_on_sites_and_groups() -> None:
    async with harness(entitlements=_home()) as h:
        owner = await h.owner()
        await h.register(owner)  # creates the hidden default site: 1 of 1
        site = await h.client.post(f"{API}/sites", headers=owner.headers, json={"name": "Barn"})
        assert site.status_code == 409
        assert site.json()["type"] == "urn:mowdeck:problem:plan-limit"
        await h.create(owner, "groups", name="North")
        group = await h.client.post(f"{API}/groups", headers=owner.headers, json={"name": "S"})
        assert group.status_code == 409
        assert group.json()["errors"][0]["parameter"] == "max_groups"


async def test_fleet_plan_without_control_refuses_commands_but_never_the_stop() -> None:
    plans = FakeEntitlements(plan="crafted", features=["view", "emergency_stop"])
    async with harness(entitlements=plans) as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        mower_id = (await h.register(owner))["id"]
        start = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=owner.headers, json=START
        )
        assert start.status_code == 403
        assert start.json()["type"] == "urn:mowdeck:problem:feature-not-in-plan"
        stop = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=owner.headers, json=STOP
        )
        assert stop.status_code == 202, stop.text
        assert [e.command.kind for e in h.commands_published()] == ["emergency_stop"]


async def test_fleet_grace_keeps_the_emergency_stop_and_blocks_growth() -> None:
    plans = FakeEntitlements(plan="home", status=PlanStatus.GRACE)
    async with harness(entitlements=plans) as h:
        owner = await h.owner()
        plans.status = PlanStatus.ACTIVE
        mower_id = (await h.register(owner))["id"]
        site_id = (await h.create(owner, "sites", name="Barn"))["id"]
        plans.status = PlanStatus.GRACE
        stop = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=owner.headers, json=STOP
        )
        assert stop.status_code == 202
        start = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=owner.headers, json=START
        )
        assert start.status_code == 403
        assert "not active" in start.json()["detail"]
        more = await h.client.post(
            f"{API}/mowers",
            headers=owner.headers,
            json={"name": "x", "connection_kind": "simulated"},
        )
        assert more.status_code == 409
        bulk_stop = await h.client.post(
            f"{API}/sites/{site_id}/commands", headers=owner.headers, json=STOP
        )
        assert bulk_stop.status_code == 202
        listed = await h.client.get(f"{API}/mowers", headers=owner.headers)
        assert listed.status_code == 200  # viewing always works


async def test_fleet_bulk_commands_need_the_bulk_feature() -> None:
    plans = _home()
    async with harness(entitlements=plans) as h:
        owner = await h.owner()
        group_id = (await h.create(owner, "groups", name="North"))["id"]
        refused = await h.client.post(
            f"{API}/groups/{group_id}/commands", headers=owner.headers, json=START
        )
        assert refused.status_code == 403
        assert refused.json()["errors"][0]["parameter"] == "fleet.bulk_commands"
        stop = await h.client.post(
            f"{API}/groups/{group_id}/commands", headers=owner.headers, json=STOP
        )
        assert stop.status_code == 202
        plans.features.append("fleet.bulk_commands")
        allowed = await h.client.post(
            f"{API}/groups/{group_id}/commands", headers=owner.headers, json=START
        )
        assert allowed.status_code == 202


async def test_fleet_issue_on_behalf_reports_not_in_plan() -> None:
    plans = FakeEntitlements(plan="crafted", features=["view"])
    async with harness(entitlements=plans) as h:
        owner = await h.owner()
        mower_id = UUID((await h.register(owner))["id"])
        request = IssueCommandRequest(
            on_behalf_of=PrincipalRef(subject=owner.principal.subject, kind=PrincipalKind.USER),
            account_id=owner.account_id,
            mower_id=mower_id,
            command=CommandRequest(command=StartMow(area_ids=["front"])),
        )
        reply = (await h.bus.request(FLEET_COMMANDS_ISSUE, request)).model(IssueCommandReply)
    assert reply.error is not None
    assert reply.error.code is IssueCommandErrorCode.NOT_IN_PLAN


async def test_fleet_asks_entitlements_over_the_bus_and_drops_its_cache() -> None:
    async with harness() as h:
        asked: list[EntitlementCheckRequest] = []

        async def answer(message: BusMessage) -> None:
            request = message.model(EntitlementCheckRequest)
            asked.append(request)
            snapshot = EntitlementSnapshot(
                account_id=request.account_id,
                plan="home",
                limits=PlanLimits(max_mowers=1),
                features=["control"],
                billing_enabled=True,
            )
            await message.respond(snapshot.decide(request.feature, request.limit))

        await h.bus.subscribe(ENTITLEMENTS_CHECK, answer, queue="entitlements")
        owner = await h.owner()
        await h.register(owner)
        second = await h.client.post(
            f"{API}/mowers",
            headers=owner.headers,
            json={"name": "b", "connection_kind": "simulated"},
        )
        assert second.status_code == 409
        assert len(asked) == 1  # the second question came from the cache
        client = h.app.state.entitlements
        assert isinstance(client, EntitlementsClient)
        assert len(client) == 1
        client.invalidate()
        assert len(client) == 0
