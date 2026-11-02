"""Structured answers (``POST /me/provider/structured``, ADR 0075): the interface optional
modules use to reuse the caller's AI provider. Resolution, limits, accounting, the fake's
minimal answer and the LiteLLM request shape (mocked ``acompletion``, no network)."""

import json
from datetime import timedelta
from typing import Any

import pytest
from test_vision_llm import Scripted, _module, _no_sleep, _response
from vision_support import API, T0, harness

from vision.adapters.providers.fake import FakeVisionProvider, minimal_instance
from vision.adapters.providers.llm import LiteLlmProvider
from vision.domain.errors import ProviderFailed
from vision.domain.models import ProviderConfig

URL = f"{API}/me/provider/structured"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["actions", "reply"],
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind"],
                "properties": {"kind": {"type": "string", "enum": ["dock", "pause"]}},
            },
        },
        "reply": {"type": "string"},
    },
}


def _body(**overrides: Any) -> dict[str, Any]:
    return {
        "purpose": "assistant",
        "system": "You turn requests into actions.",
        "text": "send it home",
        "schema": SCHEMA,
        **overrides,
    }


async def test_vision_structured_answers_with_the_hosted_fake_and_records_the_call() -> None:
    async with harness() as h:
        owner = h.owner()
        response = await h.client.post(URL, json=_body(), headers=owner.headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["output"] == {"actions": [], "reply": ""}
        assert body["model"] == "fake/recover"
        assert body["hosted"] is True
        used = await h.store.count_requests(
            since=T0 - timedelta(days=3650), kind="structured", user_id=owner.principal.subject
        )
        assert used == 1


async def test_vision_structured_has_its_own_hourly_limit() -> None:
    async with harness(vision_structured_per_hour=1) as h:
        owner = h.owner()
        assert (await h.client.post(URL, json=_body(), headers=owner.headers)).status_code == 200
        second = await h.client.post(URL, json=_body(), headers=owner.headers)
        assert second.status_code == 429
        assert second.json()["type"].endswith("vision-quota")


async def test_vision_structured_without_a_provider_is_409() -> None:
    async with harness(vision_default_enabled=False) as h:
        owner = h.owner()
        response = await h.client.post(URL, json=_body(), headers=owner.headers)
        assert response.status_code == 409
        assert response.json()["type"].endswith("vision-provider-missing")


async def test_vision_structured_refuses_bad_requests() -> None:
    async with harness() as h:
        owner = h.owner()
        not_object = _body(schema={"type": "array"})
        assert (await h.client.post(URL, json=not_object, headers=owner.headers)).status_code == 422
        bad_purpose = _body(purpose="Assistant!")
        assert (
            await h.client.post(URL, json=bad_purpose, headers=owner.headers)
        ).status_code == 422
        too_long = _body(text="x" * 4001)
        assert (await h.client.post(URL, json=too_long, headers=owner.headers)).status_code == 422
        smuggled = {**_body(), "model": "openai/gpt-x"}
        assert (await h.client.post(URL, json=smuggled, headers=owner.headers)).status_code == 422
        anonymous = await h.client.post(URL, json=_body())
        assert anonymous.status_code == 401


def test_vision_structured_minimal_instance_follows_the_schema() -> None:
    schema = {
        "type": "object",
        "required": ["a", "b", "c", "d", "e"],
        "properties": {
            "a": {"type": "integer", "minimum": 3},
            "b": {"type": ["string", "null"]},
            "c": {"type": "array", "items": {"type": "boolean"}, "minItems": 1},
            "d": {"const": "x"},
            "e": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
        },
    }
    assert minimal_instance(schema) == {"a": 3, "b": None, "c": [False], "d": "x", "e": False}


async def test_vision_structured_fake_fail_mode_fails() -> None:
    provider = FakeVisionProvider("fake/fail")
    with pytest.raises(ProviderFailed):
        await provider.structured("s", "t", SCHEMA, "answer")


async def test_vision_structured_litellm_sends_a_strict_schema_and_a_text_message() -> None:
    answer = {"actions": [{"kind": "dock"}], "reply": "Sending it home."}
    completion = Scripted(_response(json.dumps(answer)))
    provider = LiteLlmProvider(
        ProviderConfig(model="openai/gpt-x", family="openai", hosted=False, api_key="sk-test"),
        completion=completion,
        litellm_module=_module(),
        sleep=_no_sleep,
        max_tokens=2000,
    )
    result = await provider.structured("SYSTEM", "send it home", SCHEMA, "assistant_plan")
    assert result.output == answer
    [call] = completion.calls
    assert call["max_tokens"] == 2000
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["response_format"]["json_schema"]["name"] == "assistant_plan"
    assert call["response_format"]["json_schema"]["schema"] == SCHEMA
    system, user = call["messages"]
    assert system == {"role": "system", "content": "SYSTEM"}
    assert user["content"] == [{"type": "text", "text": "send it home"}]


async def test_vision_structured_litellm_without_schema_support_asks_in_the_prompt() -> None:
    completion = Scripted(_response("Sure:\n" + json.dumps({"actions": [], "reply": "ok"})))
    provider = LiteLlmProvider(
        ProviderConfig(model="ollama/small", family="ollama", hosted=False),
        completion=completion,
        litellm_module=_module(schema=False),
        sleep=_no_sleep,
    )
    result = await provider.structured("SYSTEM", "hello", SCHEMA, "answer")
    assert result.output == {"actions": [], "reply": "ok"}
    [call] = completion.calls
    assert "response_format" not in call
    assert "matches this JSON schema" in call["messages"][0]["content"]
    assert result.usage.mode == "prompt"
