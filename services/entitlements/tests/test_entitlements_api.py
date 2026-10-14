"""HTTP API of entitlements: resolution with billing on and off, trial and grace transitions,
operator endpoints, cross-account isolation."""

from typing import Any
from uuid import uuid4

from entitlements_support import API, harness
from mowdeck_contracts.v1 import (
    ENTITLEMENTS_PLAN_CHANGED,
    MODULE_DESCRIPTOR_PATH,
    DenyReason,
    EntitlementCheckRequest,
    EventEnvelope,
    PlanChanged,
    PrincipalKind,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type

from entitlements.module import DESCRIPTOR

PLAN_CHANGED = event_subject_for_type(ENTITLEMENTS_PLAN_CHANGED)


def _events(bus: Any) -> list[PlanChanged]:
    return [m.model(EventEnvelope[PlanChanged]).payload for m in bus.messages(PLAN_CHANGED)]


async def test_entitlements_health_descriptor_and_public_plans() -> None:
    async with harness() as h:
        assert (await h.client.get("/readyz")).status_code == 200
        descriptor = (await h.client.get(MODULE_DESCRIPTOR_PATH)).json()
        assert descriptor == DESCRIPTOR.model_dump(mode="json")
        assert {f["key"] for f in descriptor["features"]} == {"view", "control", "emergency_stop"}
        plans = (await h.client.get(f"{API}/plans")).json()  # no token needed
    assert [p["key"] for p in plans] == ["fleet", "home", "unlimited"]
    home = next(p for p in plans if p["key"] == "home")
    assert home["limits"]["max_mowers"] == 4
    assert home["is_default"] is True
    assert "manual_drive" in home["features"]


async def test_entitlements_me_is_unlimited_while_billing_is_disabled() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.account_created(owner.account_id)
        body = await h.me(owner)
        assert (await h.client.get(f"{API}/me")).status_code == 401
    assert body["plan"] == "unlimited"
    assert body["billing_enabled"] is False
    assert body["status"] == "active"
    assert body["features"] == ["*"]
    assert body["limits"]["max_mowers"] is None
    assert body["account_id"] == str(owner.account_id)


async def test_entitlements_me_uses_the_default_plan_with_billing() -> None:
    async with harness(entitlements_billing_enabled=True) as h:
        owner = await h.owner()
        body = await h.me(owner)  # no row yet: the default plan, active
        assert (body["plan"], body["status"], body["source"]) == ("home", "active", "default")
        assert body["billing_enabled"] is True
        await h.account_created(owner.account_id, name="Lukas")
        trial = await h.me(owner)
    assert trial["status"] == "trial"
    assert trial["trial_until"].startswith("2026-11-09")  # 30 trial days of home


async def test_entitlements_trial_and_grace_end_as_expired_with_the_clock() -> None:
    async with harness(entitlements_billing_enabled=True) as h:
        owner, operator = await h.owner(), await h.operator()
        await h.account_created(owner.account_id)
        h.clock.advance(days=29)
        assert (await h.me(owner))["status"] == "trial"
        h.clock.advance(days=2)
        expired = await h.me(owner)
        assert expired["status"] == "expired"
        reply = await h.services.entitlements.check(
            EntitlementCheckRequest(account_id=owner.account_id, feature="control")
        )
        assert (reply.allowed, reply.reason) == (False, DenyReason.PLAN_INACTIVE)
        response = await h.assign(operator, owner.account_id, plan="fleet", status="grace")
        assert response.status_code == 200, response.text
        grace = response.json()
        assert grace["effective"]["status"] == "grace"
        assert grace["assigned"]["grace_until"].startswith("2026-11-24")  # 14 grace days
        h.clock.advance(days=15)
        assert (await h.me(owner))["status"] == "expired"


async def test_entitlements_operator_endpoints_need_the_platform_relation() -> None:
    async with harness(entitlements_billing_enabled=True) as h:
        owner = await h.owner()
        token = h.person(owner.account_id, kind=PrincipalKind.API_TOKEN)
        for person in (owner, token):
            for method, path in (
                ("GET", f"{API}/accounts"),
                ("GET", f"{API}/accounts/{owner.account_id}"),
                ("GET", f"{API}/plans/home"),
            ):
                response = await h.client.request(method, path, headers=person.headers)
                assert response.status_code == 403, (path, response.text)
            denied = await h.assign(person, owner.account_id, plan="fleet")
            assert denied.status_code == 403
            put = await h.client.put(
                f"{API}/plans/home", headers=person.headers, json={"name": "Home"}
            )
            assert put.status_code == 403
        assert (await h.client.get(f"{API}/accounts")).status_code == 401


async def test_entitlements_owner_cannot_read_another_accounts_plan() -> None:
    async with harness(entitlements_billing_enabled=True) as h:
        lukas, bob, operator = await h.owner(), await h.owner(), await h.operator()
        assigned = await h.assign(operator, bob.account_id, plan="fleet")
        assert assigned.status_code == 200
        foreign = await h.client.get(f"{API}/accounts/{bob.account_id}", headers=lukas.headers)
        assert foreign.status_code == 403
        own = await h.me(lukas)
        bobs = await h.me(bob)
    assert own["plan"] == "home" and own["account_id"] == str(lukas.account_id)
    assert bobs["plan"] == "fleet"


async def test_entitlements_manual_assignment_records_history_and_event() -> None:
    async with harness(entitlements_billing_enabled=True) as h:
        owner, operator = await h.owner(), await h.operator()
        await h.account_created(owner.account_id, name="Garden")
        h.clock.advance(hours=1)
        response = await h.assign(operator, owner.account_id, plan="fleet", reason="pilot customer")
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["effective"]["plan"] == "fleet"
        assert detail["assigned"]["source"] == "manual"
        assert detail["account_name"] == "Garden"
        assert [(e["plan"], e["reason"]) for e in detail["history"]] == [
            ("fleet", "pilot customer"),
            ("home", "account created"),
        ]
        assert detail["history"][0]["actor"] == f"user:{operator.principal.subject}"
        changes = [e for e in _events(h.bus) if e.account_id == owner.account_id]
        assert [(c.plan, c.status) for c in changes] == [("fleet", "active")]
        again = await h.client.get(f"{API}/accounts/{owner.account_id}", headers=operator.headers)
        assert again.json()["effective"]["plan"] == "fleet"
        assert (await h.me(owner))["plan"] == "fleet"


async def test_entitlements_assignment_validation() -> None:
    async with harness(entitlements_billing_enabled=True) as h:
        operator = await h.operator()
        target = uuid4()
        unknown = await h.assign(operator, target, plan="gold")
        assert unknown.status_code == 422
        assert "unknown plan" in unknown.json()["detail"]
        no_trial = await h.assign(operator, target, plan="unlimited", status="trial")
        assert no_trial.status_code == 422
        blank = await h.assign(operator, target, plan="home", reason="   ")
        assert blank.status_code == 422
        naive = await h.assign(
            operator, target, plan="home", status="trial", trial_until="2026-12-01T00:00:00"
        )
        assert naive.status_code == 422
        trial = await h.assign(operator, target, plan="home", status="trial")
        assert trial.status_code == 200
        assert trial.json()["assigned"]["trial_until"].startswith("2026-11-09")
        explicit = await h.assign(
            operator,
            target,
            plan="home",
            status="trial",
            trial_until="2026-12-01T00:00:00+00:00",
        )
        assert explicit.json()["assigned"]["trial_until"].startswith("2026-12-01")


async def test_entitlements_accounts_list_is_paged() -> None:
    async with harness() as h:
        operator = await h.operator()
        ids = [uuid4() for _ in range(3)]
        for index, account_id in enumerate(ids):
            h.clock.advance(minutes=index + 1)
            await h.account_created(account_id, name=f"Account {index}")
        first = await h.client.get(f"{API}/accounts", headers=operator.headers, params={"limit": 2})
        assert first.status_code == 200
        page = first.json()
        assert (page["total"], page["offset"], page["limit"]) == (3, 0, 2)
        assert page["billing_enabled"] is False
        assert [i["account_id"] for i in page["items"]] == [str(a) for a in ids[:2]]
        item = page["items"][0]
        assert item["assigned"]["plan"] == "home"
        assert item["effective"]["plan"] == "unlimited"  # billing disabled says so
        assert set(item) == {"account_id", "account_name", "assigned", "effective"}
        rest = await h.client.get(f"{API}/accounts", headers=operator.headers, params={"offset": 2})
        assert [i["account_name"] for i in rest.json()["items"]] == ["Account 2"]


async def test_entitlements_put_plan_creates_and_replaces() -> None:
    async with harness(entitlements_billing_enabled=True) as h:
        operator, owner = await h.operator(), await h.owner()
        body = {
            "name": "No driving",
            "limits": {"max_mowers": 10},
            "features": ["view", "control", "emergency_stop", "unknown_thing"],
            "is_default": True,
        }
        created = await h.client.put(f"{API}/plans/no-drive", headers=operator.headers, json=body)
        assert created.status_code == 200, created.text
        plan = created.json()
        assert plan["unknown_features"] == ["unknown_thing"]
        assert plan["from_plan_file"] is False
        assert plan["is_default"] is True
        events = _events(h.bus)
        assert events[-1].all_accounts and events[-1].plan == "no-drive"
        home = await h.client.get(f"{API}/plans/home", headers=operator.headers)
        assert home.json()["is_default"] is False  # one default only
        assert home.json()["from_plan_file"] is True
        assert home.json()["unknown_features"] == [  # no module descriptor fetched here
            "ai_recover",
            "camera",
            "health",
            "home_assistant",
            "manual_drive",
            "map",
            "notifications",
            "schedules",
            "spot_mowing",
        ]
        missing = await h.client.get(f"{API}/plans/gold", headers=operator.headers)
        assert missing.status_code == 404
        invalid = await h.client.put(
            f"{API}/plans/home", headers=operator.headers, json={"name": "x", "features": ["A"]}
        )
        assert invalid.status_code == 422
        retired = await h.client.put(
            f"{API}/plans/fleet", headers=operator.headers, json={"name": "Fleet", "active": False}
        )
        assert retired.status_code == 200
        offered = [p["key"] for p in (await h.client.get(f"{API}/plans")).json()]
        assert offered == ["home", "no-drive", "unlimited"]
        assert (await h.me(owner))["plan"] == "home"  # the setting names home


async def test_entitlements_features_catalogue() -> None:
    async with harness() as h:
        owner = await h.owner()
        response = await h.client.get(f"{API}/features", headers=owner.headers)
        assert (await h.client.get(f"{API}/features")).status_code == 401
    assert response.status_code == 200
    assert [f["key"] for f in response.json()] == ["control", "emergency_stop", "view"]
    assert {f["module"] for f in response.json()} == {"entitlements"}


async def test_entitlements_unknown_default_plan_denies_new_things() -> None:
    async with harness(entitlements_billing_enabled=True, entitlements_default_plan="gold") as h:
        owner = await h.owner()
        body = await h.me(owner)  # falls back to the plan marked is_default
        assert body["plan"] == "home"
    async with harness(
        entitlements_billing_enabled=True, entitlements_default_plan="gold", plan_specs=[]
    ) as h:
        owner = await h.owner()
        body = await h.me(owner)
    assert body["plan"] == "gold"
    assert body["features"] == []
    assert body["limits"]["max_mowers"] == 0
