"""Hardening headers on every service response: nosniff, no-store on authenticated answers."""

from collections.abc import AsyncIterator

from fastapi import APIRouter, Response
from fastapi.responses import StreamingResponse
from mowdeck_service_kit import NotFound
from mowdeck_service_kit.testing import lifespan_client, make_test_app

router = APIRouter()


@router.get("/thing")
async def thing() -> dict[str, str]:
    return {"ok": "yes"}


@router.get("/tile")
async def tile(response: Response) -> dict[str, str]:
    response.headers["Cache-Control"] = "private, max-age=86400"
    return {"tile": "yes"}


@router.get("/missing")
async def missing() -> None:
    raise NotFound("nothing here")


@router.get("/stream")
async def stream() -> StreamingResponse:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"a"
        yield b"b"

    return StreamingResponse(chunks(), media_type="application/octet-stream")


AUTH = {"Authorization": "Bearer x"}


async def test_every_response_is_nosniff() -> None:
    async with lifespan_client(make_test_app(routers=[router])) as client:
        for path in ("/api/v1/thing", "/api/v1/missing", "/api/v1/stream", "/healthz", "/nope"):
            response = await client.get(path)
            assert response.headers["x-content-type-options"] == "nosniff", path


async def test_authenticated_answers_are_not_stored() -> None:
    async with lifespan_client(make_test_app(routers=[router])) as client:
        signed_in = await client.get("/api/v1/thing", headers=AUTH)
        error = await client.get("/api/v1/missing", headers=AUTH)
        streamed = await client.get("/api/v1/stream", headers=AUTH)
        anonymous = await client.get("/api/v1/thing")
    assert signed_in.headers["cache-control"] == "no-store"
    assert error.headers["cache-control"] == "no-store"
    assert streamed.headers["cache-control"] == "no-store"
    assert streamed.content == b"ab"
    assert "cache-control" not in anonymous.headers


async def test_a_route_keeps_its_own_cache_control() -> None:
    async with lifespan_client(make_test_app(routers=[router])) as client:
        response = await client.get("/api/v1/tile", headers=AUTH)
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert response.headers["x-content-type-options"] == "nosniff"
