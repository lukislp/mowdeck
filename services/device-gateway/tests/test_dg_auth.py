"""The mower listing with real access tokens (mowdeck-auth JWT resolver)."""

from uuid import uuid4

from dg_support import make_app, registration, telemetry, wait_until
from mowdeck_auth import JwtPrincipalResolver
from mowdeck_auth.testing import TEST_JWKS_URL, TestKeys, bearer
from mowdeck_contracts.v1 import Principal, PrincipalKind
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.main import default_principal_resolver
from device_gateway.module import DESCRIPTOR, VIEW_PERMISSION

MOWERS = "/api/v1/device-gateway/mowers"


def test_resolver_comes_from_the_environment() -> None:
    assert default_principal_resolver({}) is None
    resolver = default_principal_resolver({"MOWDECK_AUTH_JWKS_URL": TEST_JWKS_URL})
    assert isinstance(resolver, JwtPrincipalResolver)


def test_view_permission_follows_mower_view_on_the_account() -> None:
    assert DESCRIPTOR.permissions[0].implied_by == ["mower.view"]


async def test_listing_with_a_bearer_token() -> None:
    keys = TestKeys()
    bus = InMemoryBus()
    account_id = uuid4()
    mine = registration(account_id=account_id, name="Mine")
    app = make_app(
        bus=bus, principal_resolver=keys.resolver(), bootstrap_mowers=[mine.model_dump(mode="json")]
    )
    anna = Principal(
        subject=uuid4(),
        account_id=account_id,
        kind=PrincipalKind.SUBUSER,
        permissions=["mower.view", VIEW_PERMISSION],
    )
    viewer_elsewhere = Principal(
        subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER, permissions=[VIEW_PERMISSION]
    )
    group_operator = Principal(
        subject=uuid4(),
        account_id=account_id,
        kind=PrincipalKind.SUBUSER,
        permissions=["mower.view"],
    )
    async with lifespan_client(app) as client:
        await wait_until(lambda: telemetry(bus, mine.mower_id))
        ok = await client.get(MOWERS, headers=bearer(keys.mint(anna)))
        anonymous = await client.get(MOWERS)
        forged = await client.get(MOWERS, headers=bearer(TestKeys().mint(anna)))
        foreign = await client.get(MOWERS, headers=bearer(keys.mint(viewer_elsewhere)))
        without_key = await client.get(MOWERS, headers=bearer(keys.mint(group_operator)))
    assert ok.status_code == 200
    assert [m["mower_id"] for m in ok.json()["mowers"]] == [str(mine.mower_id)]
    assert anonymous.status_code == 401
    assert forged.status_code == 401
    assert foreign.status_code == 200 and foreign.json()["mowers"] == []
    assert without_key.status_code == 403
