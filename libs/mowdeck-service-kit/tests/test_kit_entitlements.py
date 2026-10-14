"""The Entitlements client: cache, fallback, invalidation, problem types and the gate."""

from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import APIRouter, Depends
from mowdeck_contracts.v1 import (
    ALL_FEATURES,
    ENTITLEMENTS_PLAN_CHANGED,
    DenyReason,
    EntitlementCheckRequest,
    EntitlementSnapshot,
    EventEnvelope,
    PlanChanged,
    PlanLimits,
    PlanStatus,
    Principal,
)
from mowdeck_contracts.v1.subjects import ENTITLEMENTS_CHECK
from mowdeck_service_kit.bus import BusMessage, InMemoryBus, publish_event
from mowdeck_service_kit.entitlements import (
    EntitlementsClient,
    FeatureNotInPlan,
    PlanLimitReached,
    client_from_settings,
    feature_gate,
    install_entitlements,
    require_feature,
    require_limit,
)
from mowdeck_service_kit.testing import (
    FakeEntitlements,
    lifespan_client,
    make_principal,
    make_test_app,
    make_test_settings,
)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def _responder(bus: InMemoryBus, snapshot: EntitlementSnapshot) -> list[object]:
    asked: list[object] = []

    async def handle(message: BusMessage) -> None:
        request = message.model(EntitlementCheckRequest)
        asked.append(request)
        own = snapshot.model_copy(update={"account_id": request.account_id})
        await message.respond(own.decide(request.feature, request.limit))

    await bus.subscribe(ENTITLEMENTS_CHECK, handle, queue="entitlements")
    return asked


def _home(**values: object) -> EntitlementSnapshot:
    base: dict[str, object] = {
        "account_id": uuid4(),
        "plan": "home",
        "limits": PlanLimits(max_mowers=4),
        "features": ["control", "manual_drive"],
        "billing_enabled": True,
    }
    return EntitlementSnapshot.model_validate({**base, **values})


async def test_kit_entitlements_client_caches_the_snapshot() -> None:
    bus, clock = InMemoryBus(), Clock()
    asked = await _responder(bus, _home())
    client = EntitlementsClient(bus, billing_enabled=True, cache_seconds=60, clock=clock)
    account_id = uuid4()
    assert (await client.check_feature(account_id, "manual_drive")).allowed
    denied = await client.check_feature(account_id, "reports")
    assert (denied.allowed, denied.reason) == (False, DenyReason.FEATURE_NOT_IN_PLAN)
    limit = await client.check_limit(account_id, "max_mowers", 4)
    assert (limit.allowed, limit.limit, limit.current) == (False, 4, 4)
    assert await client.plan_of(account_id) == "home"
    assert len(asked) == 1 and len(client) == 1
    clock.now += 61
    assert (await client.check_limit(account_id, "max_mowers", 3)).allowed
    assert len(asked) == 2


async def test_kit_entitlements_client_never_asks_for_view_or_stop() -> None:
    client = EntitlementsClient(None, billing_enabled=True)
    for feature in ("view", "emergency_stop"):
        assert (await client.check_feature(uuid4(), feature)).allowed


@pytest.mark.parametrize("with_bus", [True, False])
async def test_kit_entitlements_fallback_follows_the_billing_switch(with_bus: bool) -> None:
    bus = InMemoryBus() if with_bus else None  # no responder: NoRespondersError
    account_id = uuid4()
    open_client = EntitlementsClient(bus, billing_enabled=False)
    reply = await open_client.check_limit(account_id, "max_mowers", 99)
    assert (reply.allowed, reply.plan, reply.current) == (True, "unlimited", 99)
    assert await open_client.plan_of(account_id) == "unlimited"
    closed = EntitlementsClient(bus, billing_enabled=True)
    refused = await closed.check_feature(account_id, "manual_drive")
    assert (refused.allowed, refused.reason) == (False, DenyReason.UNAVAILABLE)
    assert await closed.plan_of(account_id) is None
    assert len(closed) == 0  # an outage is never cached


async def test_kit_entitlements_plan_changed_drops_cached_snapshots() -> None:
    bus = InMemoryBus()
    asked = await _responder(bus, _home())
    client = EntitlementsClient(bus, billing_enabled=True)
    subscription = await client.subscribe()
    assert subscription is not None
    first, second = uuid4(), uuid4()
    await client.check_feature(first, "control")
    await client.check_feature(second, "control")
    assert len(client) == 2
    await publish_event(
        bus,
        EventEnvelope[PlanChanged](
            type=ENTITLEMENTS_PLAN_CHANGED,
            producer="test",
            account_id=first,
            payload=PlanChanged(account_id=first, plan="fleet", status=PlanStatus.ACTIVE),
        ),
    )
    assert len(client) == 1
    client.apply(PlanChanged(all_accounts=True))
    assert len(client) == 0
    await client.check_feature(first, "control")
    client.invalidate(first)
    assert len(client) == 0 and len(asked) == 3
    await subscription.unsubscribe()
    assert await EntitlementsClient(None).subscribe() is None


async def test_kit_entitlements_malformed_reply_falls_back() -> None:
    bus = InMemoryBus()

    async def handle(message: BusMessage) -> None:
        await message.respond(b"not json")

    await bus.subscribe(ENTITLEMENTS_CHECK, handle)
    client = EntitlementsClient(bus, billing_enabled=False)
    assert (await client.check_feature(uuid4(), "reports")).allowed


async def test_kit_entitlements_require_helpers_raise_problems() -> None:
    fake = FakeEntitlements(plan="home", features=["control"], limits=PlanLimits(max_sites=1))
    account_id = uuid4()
    with pytest.raises(PlanLimitReached, match="cannot be checked") as outage:
        await EntitlementsClient(None, billing_enabled=True).require_limit(
            account_id, "max_mowers", 0
        )
    assert outage.value.status == 409
    with pytest.raises(FeatureNotInPlan, match="cannot be checked"):
        await EntitlementsClient(None, billing_enabled=True).require_feature(account_id, "maps")
    with pytest.raises(PlanLimitReached) as reached:
        await require_limit(fake, account_id, "max_sites", 1)
    problem = reached.value.to_problem()
    assert problem.type == "urn:mowdeck:problem:plan-limit"
    assert problem.errors is not None
    assert {(e.parameter, e.code, e.detail) for e in problem.errors} == {
        ("max_sites", "limit", "1"),
        ("max_sites", "current", "1"),
    }
    with pytest.raises(FeatureNotInPlan) as missing:
        await require_feature(fake, account_id, "manual_drive")
    assert missing.value.to_problem().type == "urn:mowdeck:problem:feature-not-in-plan"
    fake.status = PlanStatus.GRACE
    with pytest.raises(FeatureNotInPlan, match="not active"):
        await require_feature(fake, account_id, "control")
    with pytest.raises(PlanLimitReached, match="not active"):
        await require_limit(fake, account_id, "max_sites", 0)
    await require_feature(fake, account_id, "emergency_stop")
    assert fake.checks[0] == ("limit", "max_sites", 1)
    fake.invalidate(account_id)
    assert fake.invalidated == [account_id]
    assert await fake.plan_of(account_id) == "home"


def test_kit_entitlements_client_from_settings() -> None:
    settings = make_test_settings(
        entitlements_billing_enabled=True,
        entitlements_timeout_seconds=0.5,
        entitlements_cache_seconds=0,
    )
    client = client_from_settings(settings)
    assert (client.billing_enabled, client.timeout_seconds, client.cache_seconds) == (
        True,
        0.5,
        0.0,
    )
    client.attach(InMemoryBus())
    assert client.bus is not None
    assert make_test_settings().entitlements_billing_enabled is False


async def test_kit_entitlements_feature_gate_dependency() -> None:
    router = APIRouter()
    gated_principal = Annotated[Principal, Depends(feature_gate("notifications.push"))]

    @router.get("/gated")
    async def gated(principal: gated_principal) -> dict[str, str]:
        return {"subject": str(principal.subject)}

    owner = make_principal(permissions=[])
    app = make_test_app(routers=[router], principal=owner)
    async with lifespan_client(app) as client:
        assert (await client.get("/api/v1/gated")).status_code == 200  # no gate installed
        fake = FakeEntitlements(plan="home", features=["control"])
        install_entitlements(app, fake)
        denied = await client.get("/api/v1/gated")
        assert denied.status_code == 403
        assert denied.json()["type"] == "urn:mowdeck:problem:feature-not-in-plan"
        fake.features = ["notifications"]
        assert (await client.get("/api/v1/gated")).status_code == 200
        fake.features = [ALL_FEATURES]
        assert (await client.get("/api/v1/gated")).status_code == 200
