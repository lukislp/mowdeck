"""The LiteLLM provider: request shape per model family, structured output with fallback,
validation retry, transient retries and cost accounting. No network: a local HTTP server
replays recorded provider responses (tests/fixtures) through the real LiteLLM code path, and
mocked ``acompletion`` calls cover the control flow."""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mowdeck_contracts.v1 import VisionTask
from mowdeck_control import flat_png

from vision.adapters.providers import llm
from vision.adapters.providers.llm import LiteLlmProvider, load_litellm
from vision.adapters.providers.prompt import ASSESSMENT_SCHEMA, extract_json, system_prompt
from vision.domain.context import Motion, SceneContext
from vision.domain.errors import ProviderFailed
from vision.domain.families import FAMILIES
from vision.domain.models import Image, ProviderConfig, Situation

FIXTURES = Path(__file__).parent / "fixtures"
IMAGE = Image(flat_png(16, 12, (74, 140, 66)), "image/png")
CONTEXT = SceneContext(
    task=VisionTask.RECOVER, motion=Motion(linear_mps_at_1=0.5, turn_deg_per_s_at_1=57), images=1
)
ANSWER = {
    "situation": "stuck_on_obstacle",
    "confidence": 0.8,
    "proposal": [{"linear": -0.5, "angular": 0, "seconds": 1}],
    "rationale": "Back off.",
    "risk": [],
    "abort_reasons": [],
}


@contextmanager
def replay(fixture: str) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """A local server answering every POST with the recorded response; yields its base URL and
    the recorded requests (path, headers, JSON body)."""
    body = (FIXTURES / fixture).read_bytes()
    seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            seen.append({"path": self.path, "headers": dict(self.headers), "json": payload})
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


async def test_vision_llm_anthropic_recorded_response_and_request_shape() -> None:
    with replay("anthropic_messages_response.json") as (base, seen):
        provider = LiteLlmProvider(
            ProviderConfig(
                model="anthropic/claude-sonnet-5-5",
                family="anthropic",
                hosted=True,
                api_base=base,
                api_key="sk-ant-test-key",
            ),
            retries=0,
        )
        assessment = await provider.analyse([IMAGE], CONTEXT, VisionTask.RECOVER)
    assert assessment.result.situation is Situation.STUCK_ON_OBSTACLE
    assert [s.linear for s in assessment.result.proposal] == [-0.6, 0.0, 0.5]
    usage = assessment.usage
    assert (usage.input_tokens, usage.output_tokens) == (1834, 212)
    assert usage.mode == "json_schema"
    assert usage.cost_usd == pytest.approx(1834 * 2e-6 + 212 * 10e-6, rel=1e-6)
    [request] = seen
    assert request["path"].endswith("/v1/messages")
    assert request["headers"]["x-api-key"] == "sk-ant-test-key"
    body = request["json"]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 16000
    assert "temperature" not in body, "sampling parameters are rejected by current Claude models"
    assert "tool_choice" not in body, "forced tool use is rejected by Claude Sonnet 5.5"
    assert "MowDeck" in json.dumps(body["system"])
    [message] = body["messages"]
    kinds = [part["type"] for part in message["content"]]
    assert kinds == ["text", "image"]
    image = message["content"][1]["source"]
    assert image["type"] == "base64" and image["media_type"] == "image/png"
    structured = body.get("output_format") or body.get("output_config", {}).get("format")
    assert structured is not None and structured["type"] == "json_schema"
    assert set(structured["schema"]["properties"]) == set(ASSESSMENT_SCHEMA["properties"])


async def test_vision_llm_openai_compatible_endpoint_request_shape() -> None:
    with replay("openai_chat_response.json") as (base, seen):
        provider = LiteLlmProvider(
            ProviderConfig(
                model="openai/my-vision-model",
                family="openai",
                hosted=False,
                api_base=f"{base}/v1",
                api_key="sk-local",
                headers={"X-Tenant": "garden"},
            ),
            retries=0,
        )
        assessment = await provider.analyse([IMAGE], CONTEXT, VisionTask.RECOVER)
    assert assessment.usage.input_tokens == 905
    assert assessment.usage.cost_usd is None, "an unknown model has no price"
    [request] = seen
    assert request["path"] == "/v1/chat/completions"
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert headers["authorization"] == "Bearer sk-local"
    assert headers["x-tenant"] == "garden"
    body = request["json"]
    assert body["model"] == "my-vision-model"
    user = body["messages"][1]["content"]
    assert user[1]["type"] == "image_url"
    assert user[1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_vision_llm_ollama_local_model_uses_prompt_mode() -> None:
    with replay("ollama_generate_response.json") as (base, seen):
        provider = LiteLlmProvider(
            ProviderConfig(model="ollama/llava", family="ollama", hosted=False, api_base=base),
            retries=0,
        )
        assessment = await provider.analyse([IMAGE], CONTEXT, VisionTask.RECOVER)
    assert assessment.usage.mode == "prompt"
    assert assessment.result.confidence == pytest.approx(0.82)
    [request] = seen
    assert request["path"].startswith("/api/")
    body = request["json"]
    assert body["model"] == "llava"
    text = json.dumps(body)
    assert "matches this JSON schema" in text, "the schema goes into the prompt"
    assert body.get("images"), "the image is sent to Ollama"


class _Errors:
    """A stand-in for ``litellm.exceptions`` (the real classes need a provider context)."""

    class APIError(Exception):
        pass

    class Timeout(APIError):  # noqa: N818 - the name LiteLLM uses
        pass

    class APIConnectionError(APIError):
        pass

    class RateLimitError(APIError):
        pass

    class InternalServerError(APIError):
        pass

    class ServiceUnavailableError(APIError):
        pass

    class BadGatewayError(APIError):
        pass

    class BadRequestError(APIError):
        status_code = 400

    class AuthenticationError(APIError):
        pass

    class PermissionDeniedError(APIError):
        pass

    class NotFoundError(APIError):
        pass

    class ContentPolicyViolationError(APIError):
        pass

    class ContextWindowExceededError(APIError):
        pass

    class UnprocessableEntityError(APIError):
        pass


def _response(text: str, prompt: int = 100, completion: int = 20) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def _module(schema: bool = True, cost: float | None = 0.001) -> Any:
    def completion_cost(**_: Any) -> float:
        if cost is None:
            raise ValueError("no price")
        return cost

    return SimpleNamespace(
        exceptions=_Errors,
        supports_response_schema=lambda model: schema,
        completion_cost=completion_cost,
        acompletion=None,
    )


class Scripted:
    """A mocked ``acompletion``: each call pops the next outcome (a response or an error)."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _no_sleep(_: float) -> None:
    return


def _provider(
    completion: Scripted, module: Any, model: str = "anthropic/claude-x", **kw: Any
) -> LiteLlmProvider:
    return LiteLlmProvider(
        ProviderConfig(model=model, family=model.split("/")[0], hosted=False, **kw),
        completion=completion,
        litellm_module=module,
        sleep=_no_sleep,
    )


async def test_vision_llm_schema_refusal_falls_back_to_prompt_mode() -> None:
    llm._NO_SCHEMA.discard("anthropic/claude-refuses")
    completion = Scripted(_Errors.BadRequestError("schema"), _response(json.dumps(ANSWER)))
    provider = _provider(completion, _module(), model="anthropic/claude-refuses")
    assessment = await provider.analyse([IMAGE], CONTEXT, VisionTask.RECOVER)
    first, second = completion.calls
    assert "response_format" in first and "response_format" not in second
    assert "matches this JSON schema" in second["messages"][0]["content"]
    assert assessment.usage.mode == "prompt"
    # The process remembers the refusal.
    again = Scripted(_response(json.dumps(ANSWER)))
    await _provider(again, _module(), model="anthropic/claude-refuses").analyse(
        [IMAGE], CONTEXT, VisionTask.RECOVER
    )
    assert "response_format" not in again.calls[0]


async def test_vision_llm_invalid_answer_gets_one_retry_then_fails() -> None:
    fixed = Scripted(
        _response("not json at all"), _response("```json\n" + json.dumps(ANSWER) + "\n```")
    )
    assessment = await _provider(fixed, _module(cost=0.002)).analyse(
        [IMAGE], CONTEXT, VisionTask.RECOVER
    )
    assert assessment.usage.attempts == 2
    assert (assessment.usage.input_tokens, assessment.usage.output_tokens) == (200, 40)
    assert assessment.usage.cost_usd == pytest.approx(0.004)
    retry_messages = fixed.calls[1]["messages"]
    assert retry_messages[-1]["role"] == "user" and "did not match" in retry_messages[-1]["content"]
    too_many_steps = {**ANSWER, "proposal": [{"linear": 0, "angular": 0, "seconds": 1}] * 7}
    broken = Scripted(_response(json.dumps(too_many_steps)), _response('{"situation": "x"}'))
    with pytest.raises(ProviderFailed, match="not valid JSON"):
        await _provider(broken, _module()).analyse([IMAGE], CONTEXT, VisionTask.RECOVER)


async def test_vision_llm_transient_errors_are_retried_and_auth_errors_are_not() -> None:
    flaky = Scripted(
        _Errors.Timeout("t"), _Errors.RateLimitError("r"), _response(json.dumps(ANSWER))
    )
    assessment = await _provider(flaky, _module(cost=None), api_key="k").analyse(
        [IMAGE], CONTEXT, VisionTask.RECOVER
    )
    assert assessment.usage.attempts == 3
    assert assessment.usage.cost_usd is None
    assert flaky.calls[0]["api_key"] == "k" and flaky.calls[0]["num_retries"] == 0
    down = Scripted(_Errors.Timeout("t"), _Errors.Timeout("t"), _Errors.Timeout("t"))
    with pytest.raises(ProviderFailed, match="after 3 attempts"):
        await _provider(down, _module()).analyse([IMAGE], CONTEXT, VisionTask.RECOVER)
    denied = Scripted(_Errors.AuthenticationError("bad key sk-secret"))
    with pytest.raises(ProviderFailed) as info:
        await _provider(denied, _module()).analyse([IMAGE], CONTEXT, VisionTask.RECOVER)
    assert "sk-secret" not in info.value.message, "error messages never echo the key"
    failed = Scripted(_Errors.APIError("boom"))
    with pytest.raises(ProviderFailed, match="the model failed"):
        await _provider(failed, _module()).analyse([IMAGE], CONTEXT, VisionTask.RECOVER)
    bad = Scripted(_Errors.BadRequestError("x"))
    with pytest.raises(ProviderFailed, match=r"BadRequestError \(400\)"):
        await _provider(bad, _module(schema=False)).analyse([IMAGE], CONTEXT, VisionTask.RECOVER)


def test_vision_llm_prompt_helpers() -> None:
    assert extract_json('Sure! {"a": 1} thanks') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    with pytest.raises(ValueError, match="no JSON"):
        extract_json("nothing here")
    assert "JSON schema" not in system_prompt(prompt_mode=False)
    assert "blades are always off" in system_prompt(prompt_mode=False)


def test_vision_llm_capabilities_of_the_supported_families() -> None:
    module = load_litellm()
    assert module.supports_response_schema(model="anthropic/claude-sonnet-5-5")
    assert module.supports_vision(model="anthropic/claude-sonnet-5-5")
    assert module.supports_vision(model="openai/gpt-5-mini")
    assert module.supports_vision(model="gemini/gemini-2.5-flash")
    assert not module.supports_response_schema(model="ollama/llava")
    assert module.turn_off_message_logging is True


def test_vision_llm_bundled_price_map_prices_every_documented_hosted_model() -> None:
    """The loader keeps LiteLLM on its bundled price map (no download at startup): every hosted
    example model of the settings page must have a price there, or its cost reads as 0."""
    module = load_litellm()
    unpriced = []
    for family in FAMILIES:
        if family.local:
            continue
        for model in family.examples:
            prompt, completion = module.cost_per_token(
                model=model, prompt_tokens=1000, completion_tokens=100
            )
            if not prompt + completion > 0:
                unpriced.append(model)
    assert unpriced == []


def test_extract_json_rejects_an_answer_nested_too_deeply() -> None:
    # json.loads raises RecursionError; the provider retries on ValueError only.
    with pytest.raises(ValueError, match="nested too deeply"):
        extract_json("[" * 100_000)
    with pytest.raises(ValueError, match="nested too deeply"):
        extract_json('Answer: {"a":' + "[" * 100_000 + "}")
