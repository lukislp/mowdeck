"""Public demo mode (ADR 0068): the response label, the policy and the seed secret."""

from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, FastAPI
from mowdeck_service_kit import NotFound
from mowdeck_service_kit.demo import (
    DEMO_HEADER,
    DEMO_SEED_HEADER,
    DemoPolicy,
    require_demo,
    require_demo_seed,
    rule,
    seed_token_matches,
)
from mowdeck_service_kit.testing import lifespan_client, make_test_app, make_test_settings

router = APIRouter()


@router.get("/things")
async def list_things() -> list[str]:
    return ["a"]


@router.post("/things")
async def add_thing() -> dict[str, str]:
    return {"added": "yes"}


@router.delete("/things/{name}")
async def drop_thing(name: str) -> dict[str, str]:
    return {"dropped": name}


@router.post("/tokens")
async def add_token() -> dict[str, str]:
    return {"token": "secret"}


@router.get("/missing")
async def missing() -> None:
    raise NotFound("nothing here")


@router.post("/demo-only", dependencies=[Depends(require_demo)])
async def demo_only() -> dict[str, str]:
    return {"demo": "yes"}


@router.post("/seed")
async def seed(_: Annotated[None, Depends(require_demo_seed)]) -> dict[str, str]:
    return {"seeded": "yes"}


POLICY = DemoPolicy(
    blocked=(
        rule(r"/api/v1/tokens", "Access tokens are not available in the demo."),
        rule(r"/api/v1/things/[^/]+", "Nothing is deleted in the demo.", "DELETE"),
    )
)


def _app(
    *, demo: bool = True, policy: DemoPolicy | None = POLICY, seed: str | None = None
) -> FastAPI:
    settings = make_test_settings(demo_mode=demo, demo_seed_token=seed)
    return make_test_app(routers=[router], settings=settings, demo_policy=policy)


async def test_every_response_is_labelled_in_demo_mode() -> None:
    async with lifespan_client(_app()) as client:
        ok = await client.get("/api/v1/things")
        refused = await client.post("/api/v1/tokens")
        missing = await client.get("/api/v1/missing")
        health = await client.get("/healthz")
    for response in (ok, refused, missing, health):
        assert response.headers[DEMO_HEADER] == "1"


async def test_no_label_and_no_refusal_outside_demo_mode() -> None:
    async with lifespan_client(_app(demo=False)) as client:
        response = await client.post("/api/v1/tokens")
        demo_only_call = await client.post("/api/v1/demo-only")
    assert response.status_code == 200
    assert DEMO_HEADER not in response.headers
    assert demo_only_call.status_code == 404


async def test_blocked_actions_are_refused_with_the_demo_problem() -> None:
    async with lifespan_client(_app()) as client:
        token = await client.post("/api/v1/tokens")
        drop = await client.delete("/api/v1/things/a")
        add = await client.post("/api/v1/things")
    assert token.status_code == 403
    body = token.json()
    assert body["type"] == "urn:mowdeck:problem:demo-mode"
    assert body["detail"] == "Access tokens are not available in the demo."
    assert body["correlation_id"]
    assert token.headers["content-type"].startswith("application/problem+json")
    assert drop.status_code == 403
    assert add.status_code == 200  # not named: the visitor may try it


async def test_allow_only_refuses_every_other_mutation() -> None:
    policy = DemoPolicy(allow_only=(rule(r"/api/v1/things", "", "POST"),))
    async with lifespan_client(_app(policy=policy)) as client:
        add = await client.post("/api/v1/things")
        drop = await client.delete("/api/v1/things/a")
        read = await client.get("/api/v1/things")
    assert add.status_code == 200
    assert drop.status_code == 403
    assert drop.json()["detail"] == "This action is not available in the demo."
    assert read.status_code == 200


async def test_the_seed_secret_passes_the_policy() -> None:
    async with lifespan_client(_app(seed="s3cret-seed")) as client:
        wrong = await client.post("/api/v1/tokens", headers={DEMO_SEED_HEADER: "guess"})
        right = await client.post("/api/v1/tokens", headers={DEMO_SEED_HEADER: "s3cret-seed"})
    assert wrong.status_code == 403
    assert right.status_code == 200
    assert right.headers[DEMO_HEADER] == "1"


async def test_without_a_configured_secret_no_header_passes() -> None:
    async with lifespan_client(_app(seed=None)) as client:
        response = await client.post("/api/v1/tokens", headers={DEMO_SEED_HEADER: ""})
        seed_call = await client.post("/api/v1/seed", headers={DEMO_SEED_HEADER: "x"})
    assert response.status_code == 403
    assert seed_call.status_code == 403


async def test_seed_endpoints_need_demo_mode_and_the_secret() -> None:
    async with lifespan_client(_app(seed="s3cret-seed")) as client:
        without = await client.post("/api/v1/seed")
        with_secret = await client.post("/api/v1/seed", headers={DEMO_SEED_HEADER: "s3cret-seed"})
    assert without.status_code == 403
    assert with_secret.status_code == 200
    async with lifespan_client(_app(demo=False, seed="s3cret-seed")) as client:
        off = await client.post("/api/v1/seed", headers={DEMO_SEED_HEADER: "s3cret-seed"})
    assert off.status_code == 404


async def test_a_demo_service_without_policy_only_labels() -> None:
    async with lifespan_client(_app(policy=None)) as client:
        response = await client.post("/api/v1/tokens")
    assert response.status_code == 200
    assert response.headers[DEMO_HEADER] == "1"


@pytest.mark.parametrize(
    ("presented", "expected", "ok"),
    [("a", "a", True), ("a", "b", False), (None, "a", False), ("a", None, False), ("", "", False)],
)
def test_seed_token_matches(presented: str | None, expected: str | None, ok: bool) -> None:
    assert seed_token_matches(presented, expected) is ok


def test_safe_methods_always_pass() -> None:
    policy = DemoPolicy(allow_only=())
    assert policy.refusal("GET", "/api/v1/anything") is None
    assert policy.refusal("options", "/api/v1/anything") is None
    assert policy.refusal("PATCH", "/api/v1/anything") is not None


def test_extended_keeps_the_allowlist() -> None:
    base = DemoPolicy(allow_only=(rule(r"/api/v1/a", ""),))
    more = base.extended([rule(r"/api/v1/a", "no a after all", "DELETE")])
    assert more.refusal("POST", "/api/v1/a") is None
    assert more.refusal("DELETE", "/api/v1/a") == "no a after all"
    assert more.refusal("POST", "/api/v1/b") is not None
