"""The kit principal resolver: 200 with a valid token, 401 problem details otherwise."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends
from mowdeck_auth import JwtPrincipalResolver, install_auth, principal_resolver_from_env
from mowdeck_auth.testing import TEST_JWKS_URL, TestKeys, bearer
from mowdeck_contracts.v1 import Principal, PrincipalKind
from mowdeck_service_kit import create_app, current_principal
from mowdeck_service_kit.testing import lifespan_client, make_test_descriptor, make_test_settings

router = APIRouter()


@router.get("/whoami")
async def whoami(principal: Annotated[Principal, Depends(current_principal)]) -> dict[str, str]:
    return {"subject": str(principal.subject), "kind": principal.kind.value}


def app_with(resolver: JwtPrincipalResolver):  # type: ignore[no-untyped-def]
    return create_app(
        make_test_settings(), make_test_descriptor(), routers=[router], principal_resolver=resolver
    )


async def test_valid_bearer_token_resolves_the_principal() -> None:
    keys = TestKeys()
    principal = Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER)
    async with lifespan_client(app_with(keys.resolver())) as client:
        response = await client.get("/api/v1/whoami", headers=bearer(keys.mint(principal)))
    assert response.status_code == 200
    assert response.json() == {"subject": str(principal.subject), "kind": "user"}


async def test_missing_and_malformed_headers_answer_401_without_error_code() -> None:
    keys = TestKeys()
    async with lifespan_client(app_with(keys.resolver())) as client:
        for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
            response = await client.get("/api/v1/whoami", headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert response.headers["content-type"].startswith("application/problem+json")
    assert keys.server.requests == 0  # nothing to verify, nothing fetched


async def test_invalid_tokens_answer_the_same_generic_401() -> None:
    keys = TestKeys()
    other = TestKeys()
    principal = Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER)
    bodies = []
    async with lifespan_client(app_with(keys.resolver())) as client:
        for token in ("garbage", other.mint(principal)):
            response = await client.get("/api/v1/whoami", headers=bearer(token))
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'
            bodies.append(response.json()["detail"])
    assert bodies == ["invalid or expired access token"] * 2


async def test_install_auth_and_resolver_from_env() -> None:
    assert principal_resolver_from_env({}) is None
    resolver = principal_resolver_from_env({"MOWDECK_AUTH_JWKS_URL": TEST_JWKS_URL})
    assert isinstance(resolver, JwtPrincipalResolver)
    keys = TestKeys()
    app = create_app(make_test_settings(), make_test_descriptor(), routers=[router])
    installed = install_auth(app, keys.verifier())
    assert app.state.principal_resolver is installed
    principal = Principal(subject=uuid4(), kind=PrincipalKind.API_TOKEN)
    async with lifespan_client(app) as client:
        response = await client.get("/api/v1/whoami", headers=bearer(keys.mint(principal)))
    assert response.json()["kind"] == "api_token"
