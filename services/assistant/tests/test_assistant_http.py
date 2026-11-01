"""The HTTP adapters against a mocked public API (``httpx.MockTransport``): the caller's token
is forwarded to every service, the answers are mapped, and Vision's structured answers are
requested with the generated schema. No network, no real AI."""

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from assistant_support import AUTH, NOW, TOKEN, action, answer, catalog, settings
from mowdeck_service_kit.testing import StaticPrincipalResolver, lifespan_client, make_principal

from assistant.adapters.interpreters import ProviderInterpreter
from assistant.adapters.upstream import HttpPlatform
from assistant.domain.errors import (
    ProviderFailed,
    ProviderMissing,
    ProviderQuota,
    UpstreamUnavailable,
)
from assistant.main import build_app

MOWER = uuid4()


class Api:
    """A scripted public API that records every request."""

    def __init__(self, **overrides: Any) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides = overrides

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for key, response in self.overrides.items():
            if key in path:
                return response if isinstance(response, httpx.Response) else response(request)
        if path.endswith("/identity/me/permissions"):
            checks = json.loads(request.content)["checks"]
            return httpx.Response(200, json=[True] * len(checks))
        if path.endswith(f"/fleet/mowers/{MOWER}"):
            return httpx.Response(200, json={"name": "Luba", "area_names": {"back": "Hinten"}})
        if path.endswith("/map"):
            areas = [{"id": "front", "name": "Front"}, {"id": "back", "name": None}]
            return httpx.Response(200, json={"map": {"areas": areas}})
        if path.endswith("/capabilities"):
            return httpx.Response(200, json={"capabilities": {}})
        if path.endswith("/state"):
            return httpx.Response(200, json={"stale": False, "state": {"activity": "docked"}})
        if path.endswith("/vision/me/provider"):
            return httpx.Response(200, json={"effective": "own", "model": "openai/gpt-x"})
        if path.endswith("/vision/me/provider/structured"):
            plan = answer(action("start_now", ["back"]))
            return httpx.Response(200, json={"output": plan, "model": "openai/gpt-x"})
        return httpx.Response(404)


def _platform(api: Api) -> HttpPlatform:
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    return HttpPlatform(
        client,
        identity_url="http://identity:8000",
        fleet_url="http://fleet:8000/",
        telemetry_url="http://telemetry:8000",
    )


def _interpreter(api: Api) -> ProviderInterpreter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    return ProviderInterpreter(client, vision_url="http://vision:8000", timeout=5)


async def test_assistant_http_end_to_end_forwards_the_callers_token() -> None:
    api = Api()
    app = build_app(
        settings(),
        clock=lambda: NOW,
        principal_resolver=StaticPrincipalResolver(make_principal()),
        transport=httpx.MockTransport(api),
    )
    async with lifespan_client(app) as client:
        status = (await client.get("/api/v1/assistant/status", headers=AUTH)).json()
        response = await client.post(
            f"/api/v1/assistant/mowers/{MOWER}/proposals",
            json={"text": "mow the back now", "timezone": "Europe/Berlin"},
            headers=AUTH,
        )
    assert status["ready"] is True and status["model"] == "openai/gpt-x"
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["executable"] is True
    assert body["actions"][0]["areas"] == [{"id": "back", "name": "Hinten"}]
    assert {r.headers["authorization"] for r in api.requests} == {f"Bearer {TOKEN}"}
    hosts = {r.url.host for r in api.requests}
    assert hosts == {"identity", "fleet", "telemetry", "vision"}
    [structured] = [r for r in api.requests if r.url.path.endswith("/structured")]
    payload = json.loads(structured.content)
    assert payload["purpose"] == "assistant"
    assert payload["schema"]["properties"]["actions"]["items"]["properties"]["kind"]["enum"] == [
        "start_later",
        "start_now",
    ]
    assert json.loads(payload["text"])["request"] == "mow the back now"
    # Only reads and the structured answer: the assistant never sends a command or schedule.
    assert {(r.method, r.url.host) for r in api.requests if r.method != "GET"} == {
        ("POST", "identity"),
        ("POST", "vision"),
    }


async def test_assistant_http_platform_maps_missing_and_stale_data() -> None:
    api = Api(
        map=httpx.Response(404),
        capabilities=httpx.Response(404),
        state=httpx.Response(200, json={"stale": True, "state": {"activity": "mowing"}}),
    )
    platform = _platform(api)
    assert await platform.map_areas(TOKEN, MOWER) == []
    assert await platform.connected(TOKEN, MOWER) is False
    assert await platform.activity(TOKEN, MOWER) == "offline"
    hidden = _platform(Api(fleet=httpx.Response(404)))
    assert await hidden.mower(TOKEN, MOWER) is None


async def test_assistant_http_platform_fails_closed() -> None:
    broken = _platform(Api(permissions=httpx.Response(200, json=[True])))
    with pytest.raises(UpstreamUnavailable):
        await broken.relations(TOKEN, MOWER, ("can_view", "can_control"))
    down = _platform(Api(state=httpx.Response(500)))
    with pytest.raises(UpstreamUnavailable):
        await down.activity(TOKEN, MOWER)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    offline = _platform(Api(fleet=refuse))
    with pytest.raises(UpstreamUnavailable):
        await offline.mower(TOKEN, MOWER)
    odd = _platform(Api(permissions=httpx.Response(200, json=["yes", True])))
    assert await odd.relations(TOKEN, MOWER, ("can_view", "can_control")) == {
        "can_view": False,
        "can_control": True,
    }


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (409, ProviderMissing),
        (429, ProviderQuota),
        (502, ProviderFailed),
        (404, UpstreamUnavailable),
        (500, UpstreamUnavailable),
    ],
)
async def test_assistant_http_interpreter_maps_vision_errors(
    status: int, error: type[Exception]
) -> None:
    interpreter = _interpreter(Api(structured=httpx.Response(status, json={})))
    with pytest.raises(error):
        await interpreter.interpret(TOKEN, catalog(), "send it home")


async def test_assistant_http_interpreter_status() -> None:
    hosted = Api(provider=httpx.Response(200, json={"effective": "hosted", "hosted_model": "m"}))
    assert (await _interpreter(hosted).status(TOKEN)).hosted is True
    none = Api(provider=httpx.Response(200, json={"effective": "none"}))
    assert (await _interpreter(none).status(TOKEN)).reason == "no_provider"
    gone = Api(provider=httpx.Response(404))
    assert (await _interpreter(gone).status(TOKEN)).reason == "provider_unavailable"
