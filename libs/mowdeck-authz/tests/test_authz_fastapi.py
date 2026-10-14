"""FastAPI integration: require (401/403/200), require_or_not_found (404), kit delegation."""

from collections.abc import Sequence
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Request
from mowdeck_authz import Authorizer, Relation, RelationshipTuple, account, group, mower, site
from mowdeck_authz.authorizer import CheckLike
from mowdeck_authz.fastapi import (
    AuthzDependency,
    authorize,
    close_authorizer,
    install_authz,
    path_object,
    principal_account,
    require,
    require_or_not_found,
)
from mowdeck_authz.refs import principal_subject
from mowdeck_authz.testing import AllowAll, DenyAll, InMemoryAuthorizer, StaticAuthorizer
from mowdeck_contracts.v1 import Principal, PrincipalKind
from mowdeck_service_kit import CurrentPrincipal, require_permission
from mowdeck_service_kit.testing import (
    StaticPrincipalResolver,
    lifespan_client,
    make_principal,
    make_test_app,
)

MOWER = path_object("mower", "mower_id")
CanControlVisible = Annotated[Principal, Depends(require_or_not_found(Relation.CAN_CONTROL, MOWER))]
CanView = Annotated[Principal, Depends(require_or_not_found(Relation.CAN_VIEW, MOWER))]
CanControl403 = Annotated[Principal, Depends(require(Relation.CAN_CONTROL, MOWER))]
CanManageUsers = Annotated[
    Principal, Depends(require(Relation.CAN_MANAGE_USERS, principal_account))
]
CanManageMowers = Annotated[Principal, Depends(require_permission("fleet.manage"))]

router = APIRouter()


@router.post("/mowers/{mower_id}/start")
async def start(mower_id: UUID, principal: CanControlVisible) -> dict[str, str]:
    return {"by": str(principal.subject)}


@router.get("/mowers/{mower_id}")
async def show(mower_id: UUID, principal: CanView) -> dict[str, str]:
    return {"mower": str(mower_id)}


@router.post("/mowers/{mower_id}/strict-start")
async def strict_start(mower_id: str, principal: CanControl403) -> dict[str, str]:
    return {"by": str(principal.subject)}


@router.get("/users")
async def users(principal: CanManageUsers) -> dict[str, str]:
    return {"ok": "yes"}


@router.post("/mowers")
async def create_mower(principal: CanManageMowers) -> dict[str, str]:
    return {"ok": "yes"}


@router.post("/mowers/{mower_id}/do/{relation}")
async def do(mower_id: UUID, relation: str, request: Request, hide: bool = True) -> dict[str, str]:
    principal = await authorize(request, relation, mower(mower_id), hide=hide)
    return {"by": str(principal.subject)}


@router.get("/visible-mowers")
async def visible(principal: CurrentPrincipal, authz: AuthzDependency) -> list[str]:
    return await authz.list_objects(principal_subject(principal), Relation.CAN_VIEW, "mower")


class Story:
    """Lukas owns account A with mowers m1 (group north) and m2; Anna is Operator on north."""

    def __init__(self) -> None:
        self.account_a = uuid4()
        self.m1, self.m2, self.north, self.home = uuid4(), uuid4(), uuid4(), uuid4()
        self.lukas = make_principal(account_id=self.account_a)
        self.anna = make_principal(account_id=self.account_a, kind=PrincipalKind.SUBUSER)
        self.eve = make_principal()  # another account
        a = account(self.account_a)
        north, home = group(self.north), site(self.home)
        lukas, anna = principal_subject(self.lukas), principal_subject(self.anna)
        self.authz = InMemoryAuthorizer(
            [
                RelationshipTuple(lukas, "owner", a),
                RelationshipTuple(anna, "member", a),
                RelationshipTuple(a.as_subject(), "account", home),
                RelationshipTuple(a.as_subject(), "account", north),
                RelationshipTuple(home.as_subject(), "parent", north),
                RelationshipTuple(a.as_subject(), "account", mower(self.m1)),
                RelationshipTuple(north.as_subject(), "parent", mower(self.m1)),
                RelationshipTuple(a.as_subject(), "account", mower(self.m2)),
                RelationshipTuple(home.as_subject(), "parent", mower(self.m2)),
                RelationshipTuple(anna, "grant_mower_view", north),
                RelationshipTuple(anna, "grant_mower_view", mower(self.m2)),
                RelationshipTuple(anna, "grant_mower_control", north),
            ]
        )
        self.resolver = StaticPrincipalResolver(self.lukas)

    def app(self, authorizer: Authorizer | None = None) -> FastAPI:
        app = make_test_app(routers=[router], principal_resolver=self.resolver)
        install_authz(app, authorizer or self.authz)
        return app

    def act_as(self, principal: Principal | None) -> None:
        self.resolver.principal = principal


async def test_owner_operator_and_foreign_user() -> None:
    story = Story()
    async with lifespan_client(story.app()) as client:
        owner = await client.post(f"/api/v1/mowers/{story.m1}/start")
        story.act_as(story.anna)
        operator_m1 = await client.post(f"/api/v1/mowers/{story.m1}/start")
        operator_m2 = await client.post(f"/api/v1/mowers/{story.m2}/start")
        anna_list = await client.get("/api/v1/visible-mowers")
        story.act_as(story.eve)
        foreign_start = await client.post(f"/api/v1/mowers/{story.m1}/start")
        foreign_show = await client.get(f"/api/v1/mowers/{story.m1}")
        foreign_strict = await client.post(f"/api/v1/mowers/{story.m1}/strict-start")
        story.act_as(None)
        anonymous = await client.post(f"/api/v1/mowers/{story.m1}/start")
    assert owner.status_code == 200
    assert owner.json() == {"by": str(story.lukas.subject)}
    assert operator_m1.status_code == 200
    assert operator_m2.status_code == 403  # Anna sees m2 but may not control it
    assert operator_m2.json()["type"] == "urn:mowdeck:problem:forbidden"
    assert sorted(anna_list.json()) == sorted([f"mower:{story.m1}", f"mower:{story.m2}"])
    assert foreign_start.status_code == 404  # existence is not revealed
    assert foreign_show.status_code == 404
    assert foreign_strict.status_code == 403  # require() always answers 403
    assert anonymous.status_code == 401


async def test_authorize_with_a_relation_chosen_at_runtime() -> None:
    story = Story()
    async with lifespan_client(story.app()) as client:
        story.act_as(story.anna)
        drive_m1 = await client.post(f"/api/v1/mowers/{story.m1}/do/can_drive")
        control_m1 = await client.post(f"/api/v1/mowers/{story.m1}/do/can_control")
        stop_m2 = await client.post(f"/api/v1/mowers/{story.m2}/do/can_emergency_stop")
        story.act_as(story.eve)
        hidden = await client.post(f"/api/v1/mowers/{story.m1}/do/can_emergency_stop")
        strict = await client.post(
            f"/api/v1/mowers/{story.m1}/do/can_control", params={"hide": "false"}
        )
        story.act_as(None)
        anonymous = await client.post(f"/api/v1/mowers/{story.m1}/do/can_control")
    assert drive_m1.status_code == 403  # visible, but no drive grant
    assert control_m1.status_code == 200
    assert stop_m2.status_code == 200  # can_emergency_stop equals can_view
    assert hidden.status_code == 404
    assert strict.status_code == 403
    assert anonymous.status_code == 401


async def test_invalid_ids_and_service_principals() -> None:
    story = Story()
    service = make_principal(account_id=story.account_a, kind=PrincipalKind.SERVICE)
    async with lifespan_client(story.app()) as client:
        not_a_uuid = await client.post("/api/v1/mowers/not-a-uuid/strict-start")
        story.act_as(service)
        hidden = await client.post(f"/api/v1/mowers/{story.m1}/start")
        strict = await client.post(f"/api/v1/mowers/{story.m1}/strict-start")
        perm = await client.post("/api/v1/mowers")
    assert not_a_uuid.status_code == 404
    assert hidden.status_code == 404
    assert strict.status_code == 403
    assert perm.status_code == 403


async def test_account_level_checks_and_kit_delegation() -> None:
    story = Story()
    async with lifespan_client(story.app()) as client:
        owner_users = await client.get("/api/v1/users")
        owner_create = await client.post("/api/v1/mowers")
        story.act_as(story.anna)
        anna_users = await client.get("/api/v1/users")
        anna_create = await client.post("/api/v1/mowers")
        story.act_as(make_principal(with_account=False))
        no_account = await client.get("/api/v1/users")
    assert owner_users.status_code == 200
    assert owner_create.status_code == 200  # fleet.manage -> can_manage_mowers on account:A
    assert anna_users.status_code == 403
    assert anna_create.status_code == 403
    assert no_account.status_code == 403
    assert (str(story.authz.checks[1].relation), str(story.authz.checks[1].object)) == (
        "can_manage_mowers",
        f"account:{story.account_a}",
    )


async def test_permission_checker_edge_cases() -> None:
    story = Story()
    app = story.app(AllowAll())
    checker = app.state.permission_checker
    assert await checker.has_permission(story.lukas, "fleet.manage")
    assert not await checker.has_permission(story.lukas, "mower.fly")
    assert not await checker.has_permission(make_principal(with_account=False), "mower.view")


async def test_static_authorizers_and_readiness() -> None:
    story = Story()
    deny: StaticAuthorizer = DenyAll()
    async with lifespan_client(story.app(deny)) as client:
        denied = await client.post(f"/api/v1/mowers/{story.m1}/start")
        ready = await client.get("/readyz")
    assert denied.status_code == 404
    assert ready.json()["checks"]["authz"]["status"] == "ok"
    assert [str(c.relation) for c in deny.checks] == ["can_control", "can_view"]
    assert await deny.list_objects(principal_subject(story.lukas), "can_view", "mower") == []


async def test_missing_authorizer_is_503() -> None:
    story = Story()
    app = make_test_app(routers=[router], principal_resolver=story.resolver)
    async with lifespan_client(app) as client:
        response = await client.post(f"/api/v1/mowers/{story.m1}/start")
    assert response.status_code == 503


async def test_install_without_ping_and_close_hook() -> None:
    class Bare:
        closed = False

        async def check(self, *args: object, **kwargs: object) -> bool:
            return True

        async def check_many(self, items: Sequence[CheckLike]) -> list[bool]:
            return [True] * len(items)

        async def list_objects(self, *args: object, **kwargs: object) -> list[str]:
            return []

        async def close(self) -> None:
            self.closed = True

    bare = Bare()
    app = make_test_app(routers=[router])
    install_authz(app, bare)
    assert all(c.name != "authz" for c in app.state.readiness_checks)
    await close_authorizer(app)
    assert bare.closed
    await close_authorizer(make_test_app())  # nothing installed: no error
