"""Tenancy dependencies and the cross-tenant rejection test pattern."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from mowdeck_contracts.v1 import Principal, PrincipalKind
from mowdeck_service_kit import (
    CurrentAccountId,
    CurrentPrincipal,
    NotFound,
    create_app,
    require_permission,
)
from mowdeck_service_kit.testing import (
    StaticPrincipalResolver,
    lifespan_client,
    make_principal,
    make_test_app,
    make_test_descriptor,
    make_test_settings,
)

# A tiny account-scoped "repository": mower id -> owning account id.
MOWERS: dict[UUID, UUID] = {}

router = APIRouter()
CanControl = Annotated[Principal, Depends(require_permission("mower.control"))]


@router.get("/me")
async def me(principal: CurrentPrincipal) -> dict[str, str]:
    return {"subject": str(principal.subject)}


@router.get("/account")
async def account(account_id: CurrentAccountId) -> dict[str, str]:
    return {"account_id": str(account_id)}


@router.get("/mowers/{mower_id}")
async def get_mower(mower_id: UUID, account_id: CurrentAccountId) -> dict[str, str]:
    # The account filter comes from the principal, never from the request.
    if MOWERS.get(mower_id) != account_id:
        raise NotFound("mower not found")
    return {"mower_id": str(mower_id)}


@router.post("/mowers/{mower_id}/start")
async def start(mower_id: UUID, principal: CanControl) -> dict[str, str]:
    return {"started_by": str(principal.subject)}


async def test_without_principal_is_401() -> None:
    async with lifespan_client(make_test_app(routers=[router], principal=None)) as client:
        response = await client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["type"] == "urn:mowdeck:problem:unauthorized"


async def test_default_resolver_denies_everything() -> None:
    app = create_app(make_test_settings(), make_test_descriptor(), routers=[router])
    async with lifespan_client(app) as client:
        response = await client.get("/api/v1/me")
    assert response.status_code == 401
    assert "not configured" in response.json()["detail"]


async def test_principal_and_account_are_resolved() -> None:
    principal = make_principal()
    async with lifespan_client(make_test_app(routers=[router], principal=principal)) as client:
        me_response = await client.get("/api/v1/me")
        account_response = await client.get("/api/v1/account")
    assert me_response.json() == {"subject": str(principal.subject)}
    assert account_response.json() == {"account_id": str(principal.account_id)}


async def test_principal_without_account_is_403() -> None:
    service = make_principal(kind=PrincipalKind.SERVICE, with_account=False)
    async with lifespan_client(make_test_app(routers=[router], principal=service)) as client:
        response = await client.get("/api/v1/account")
    assert response.status_code == 403
    assert response.json()["type"] == "urn:mowdeck:problem:forbidden"


async def test_require_permission() -> None:
    mower = uuid4()
    resolver = StaticPrincipalResolver(make_principal(permissions=["mower.view"]))
    app = make_test_app(routers=[router], principal_resolver=resolver)
    async with lifespan_client(app) as client:
        denied = await client.post(f"/api/v1/mowers/{mower}/start")
        resolver.principal = make_principal(permissions=["mower.view", "mower.control"])
        allowed = await client.post(f"/api/v1/mowers/{mower}/start")
    assert denied.status_code == 403
    assert "mower.control" in denied.json()["detail"]
    assert allowed.status_code == 200
    assert allowed.json() == {"started_by": str(resolver.principal.subject)}


async def test_cross_tenant_access_is_rejected() -> None:
    """The pattern every service repeats for every account-scoped endpoint."""
    owner = make_principal(permissions=["mower.view"])
    intruder = make_principal(permissions=["mower.view"])  # a different account
    assert owner.account_id is not None
    mower = uuid4()
    MOWERS[mower] = owner.account_id

    resolver = StaticPrincipalResolver(owner)
    app = make_test_app(routers=[router], principal_resolver=resolver)
    async with lifespan_client(app) as client:
        own = await client.get(f"/api/v1/mowers/{mower}")
        resolver.principal = intruder
        foreign = await client.get(f"/api/v1/mowers/{mower}")
        # Smuggling the owner's account id through the query string changes nothing.
        smuggled = await client.get(
            f"/api/v1/mowers/{mower}", params={"account_id": str(owner.account_id)}
        )
    assert own.status_code == 200
    assert foreign.status_code == 404  # existence is not revealed
    assert smuggled.status_code == 404


async def test_metrics_use_full_route_template() -> None:
    principal = make_principal()
    mower = uuid4()
    MOWERS[mower] = principal.account_id or uuid4()
    async with lifespan_client(make_test_app(routers=[router], principal=principal)) as client:
        await client.get(f"/api/v1/mowers/{mower}")
        metrics = (await client.get("/metrics")).text
    assert 'route="/api/v1/mowers/{mower_id}"' in metrics
    assert str(mower) not in metrics


class RecordingChecker:
    """A PermissionChecker that allows only ``allowed`` and records every question."""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.asked: list[tuple[UUID, str]] = []

    async def has_permission(self, principal: Principal, permission: str) -> bool:
        self.asked.append((principal.subject, permission))
        return permission in self.allowed


async def test_require_permission_delegates_to_installed_checker() -> None:
    # The token says mower.control, but the installed checker has the last word.
    principal = make_principal(permissions=["mower.control"])
    app = make_test_app(routers=[router], principal=principal)
    assert app.state.permission_checker is None
    checker = RecordingChecker(allowed=set())
    app.state.permission_checker = checker
    async with lifespan_client(app) as client:
        denied = await client.post(f"/api/v1/mowers/{uuid4()}/start")
        checker.allowed.add("mower.control")
        allowed = await client.post(f"/api/v1/mowers/{uuid4()}/start")
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert checker.asked == [(principal.subject, "mower.control")] * 2
