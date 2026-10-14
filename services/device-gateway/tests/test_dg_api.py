"""HTTP API: operational endpoints, the module descriptor and the mower listing."""

from dg_support import make_app, registration, telemetry, wait_until
from mowdeck_contracts.v1 import MODULE_DESCRIPTOR_PATH, PrincipalKind
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import StaticPrincipalResolver, lifespan_client, make_principal

from device_gateway.module import DESCRIPTOR, VIEW_PERMISSION

MOWERS = "/api/v1/device-gateway/mowers"


async def test_health_readiness_and_module_descriptor() -> None:
    app = make_app(bus=InMemoryBus())
    async with lifespan_client(app) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert set(ready.json()["checks"]) == {"nats", "commands"}
        descriptor = (await client.get(MODULE_DESCRIPTOR_PATH)).json()
    assert descriptor == DESCRIPTOR.model_dump(mode="json")
    assert VIEW_PERMISSION in {p["key"] for p in descriptor["permissions"]}
    assert {e["type"] for e in descriptor["events"]} == {"mower.connected", "mower.disconnected"}


async def test_listing_requires_a_principal() -> None:
    app = make_app(bus=InMemoryBus())  # default resolver denies everything
    async with lifespan_client(app) as client:
        response = await client.get(MOWERS)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_listing_requires_the_permission() -> None:
    resolver = StaticPrincipalResolver(make_principal(permissions=["mower.view"]))
    app = make_app(bus=InMemoryBus(), principal_resolver=resolver)
    async with lifespan_client(app) as client:
        assert (await client.get(MOWERS)).status_code == 403


async def test_listing_shows_only_the_callers_mowers_and_everything_to_operators() -> None:
    bus = InMemoryBus()
    owner = make_principal(permissions=[VIEW_PERMISSION])
    other = make_principal(permissions=[VIEW_PERMISSION])
    operator = make_principal(
        permissions=[VIEW_PERMISSION], kind=PrincipalKind.SERVICE, with_account=False
    )
    mine = registration(account_id=owner.account_id, name="Mine")
    theirs = registration(account_id=other.account_id, name="Theirs")
    resolver = StaticPrincipalResolver(owner)
    app = make_app(
        bus=bus,
        principal_resolver=resolver,
        bootstrap_mowers=[mine.model_dump(mode="json"), theirs.model_dump(mode="json")],
    )
    async with lifespan_client(app) as client:
        await wait_until(lambda: telemetry(bus, mine.mower_id))
        response = await client.get(MOWERS)
        assert response.status_code == 200
        body = response.json()
        assert (body["shard_index"], body["shard_count"]) == (0, 1)
        assert [m["mower_id"] for m in body["mowers"]] == [str(mine.mower_id)]
        listed = body["mowers"][0]
        assert listed["name"] == "Mine"
        assert listed["connection_kind"] == "simulated"
        assert listed["status"] == "connected"
        assert listed["activity"] == "docked"
        assert listed["connected_since"] is not None
        assert listed["last_activity_at"] is not None

        resolver.principal = other
        theirs_only = (await client.get(MOWERS)).json()["mowers"]
        assert [m["mower_id"] for m in theirs_only] == [str(theirs.mower_id)]

        resolver.principal = operator
        everything = (await client.get(MOWERS)).json()["mowers"]
        assert {m["mower_id"] for m in everything} == {str(mine.mower_id), str(theirs.mower_id)}
