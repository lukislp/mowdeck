"""``VisionProvider`` on LiteLLM: one client for every vision-capable model family.

The model string (``anthropic/claude-sonnet-5-5``, ``openai/gpt-5-mini``,
``gemini/gemini-2.5-flash``, ``mistral/pixtral-large-latest``, ``ollama/llava``,
``openai/<model>`` with a base URL, ...) selects the family. Keys come from the resolved
configuration (a user's key or the installation's); without one LiteLLM reads the family's
usual environment variable (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``GEMINI_API_KEY``, ...).

* Structured output: ``response_format`` with the JSON schema when
  ``litellm.supports_response_schema`` says the model can; otherwise (and after a 400 for the
  schema) the schema goes into the prompt and the text is parsed tolerantly. An answer that does
  not validate gets exactly one more try with the validation error.
* Transient errors (timeout, connection, 429, 5xx) are retried ``retries`` times with backoff.
* Cost accounting: input and output tokens from the response and LiteLLM's cost estimate
  (``completion_cost``; None when the model has no price in LiteLLM's map).
* Redaction: images and keys are never logged; LiteLLM's own message logging is turned off.
"""

import asyncio
import importlib
import logging
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from mowdeck_contracts.v1 import VisionTask
from mowdeck_service_kit import get_logger
from pydantic import BaseModel, ValidationError

from vision.adapters.providers.prompt import (
    RESPONSE_FORMAT,
    extract_json,
    system_prompt,
    user_content,
)
from vision.domain.context import SceneContext
from vision.domain.errors import ProviderFailed
from vision.domain.models import Assessment, AssessmentOut, Image, ProviderConfig, Usage

_log = get_logger("vision.llm")

Completion = Callable[..., Awaitable[Any]]

_NO_SCHEMA: set[str] = set()
"""Models that refused the JSON schema in this process (prompt mode from then on)."""
_configured = False


_IMPORT_LOCK = threading.Lock()
_REQUIRED_ATTRIBUTES: Final = ("acompletion", "exceptions", "supports_response_schema")


def _complete(module: Any) -> bool:
    return all(hasattr(module, name) for name in _REQUIRED_ATTRIBUTES)


def _purge_litellm() -> None:
    for name in [n for n in sys.modules if n == "litellm" or n.startswith("litellm.")]:
        sys.modules.pop(name, None)


def load_litellm() -> Any:
    """Import LiteLLM (slow: done lazily or in a warm-up thread) and silence its logging.

    One import at a time: LiteLLM's package initialisation is not safe against two threads
    importing it concurrently (the warm-up thread and a first request once raced into an
    import deadlock, which left a half-initialised module without ``exceptions`` in
    ``sys.modules`` and every later call failed). A module that is missing its public
    attributes is dropped from ``sys.modules`` and imported again; an import error drops it
    too, so the next caller retries instead of inheriting the broken state.
    """
    global _configured
    with _IMPORT_LOCK:
        module = sys.modules.get("litellm")
        if module is None or not _complete(module):
            if module is not None:
                _purge_litellm()
            try:
                module = importlib.import_module("litellm")
            except Exception:
                _purge_litellm()
                raise
            if not _complete(module):
                _purge_litellm()
                msg = "litellm imported without its public API (partial initialisation)"
                raise ImportError(msg)
        if not _configured:
            for flag in ("suppress_debug_info", "turn_off_message_logging", "drop_params"):
                setattr(module, flag, True)
            for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx"):
                logging.getLogger(name).setLevel(logging.WARNING)
            _configured = True
        return module


class LiteLlmProvider:
    """One configured model behind the ``VisionProvider`` port."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        timeout_seconds: float = 90.0,
        retries: int = 2,
        max_tokens: int = 16000,
        completion: Completion | None = None,
        litellm_module: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.max_tokens = max_tokens
        self._completion = completion
        self._litellm = litellm_module
        self._sleep = sleep

    def _module(self) -> Any:
        if self._litellm is None:
            self._litellm = load_litellm()
        return self._litellm

    def _schema_supported(self) -> bool:
        if self.config.model in _NO_SCHEMA:
            return False
        try:
            return bool(self._module().supports_response_schema(model=self.config.model))
        except Exception:
            return False

    async def analyse(
        self, images: list[Image], context: SceneContext, task: VisionTask
    ) -> Assessment:
        """Ask the model; validate the answer (one retry with the error when invalid)."""
        answer, usage = await self._ask(
            lambda prompt_mode: system_prompt(prompt_mode=prompt_mode),
            user_content(images, context, task),
            AssessmentOut,
            RESPONSE_FORMAT,
        )
        return Assessment(answer, self.config.model, self.config.family, usage)

    async def _ask[T: BaseModel](
        self,
        system: Callable[[bool], str],
        content: list[dict[str, Any]],
        schema: type[T],
        response_format: dict[str, Any],
    ) -> tuple[T, Usage]:
        """One structured question: the JSON schema as ``response_format`` when the model
        supports it (else in the prompt), the answer validated against ``schema`` with one
        retry that names the validation error, usage and cost summed over the attempts."""
        module = self._module()
        completion = self._completion or module.acompletion
        use_schema = self._schema_supported()
        corrections: list[dict[str, Any]] = []
        started = time.monotonic()
        attempts = 0
        input_tokens = output_tokens = 0
        costs: list[float | None] = []
        corrected = False
        while True:
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system(not use_schema)},
                {"role": "user", "content": content},
                *corrections,
            ]
            try:
                response, tries = await self._call(
                    completion, messages, response_format if use_schema else None
                )
            except _SchemaRefused:
                _NO_SCHEMA.add(self.config.model)
                use_schema = False
                continue
            attempts += tries
            usage = getattr(response, "usage", None)
            input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
            costs.append(self._cost(module, response))
            text = _text_of(response)
            try:
                answer = schema.model_validate(extract_json(text))
            except (ValueError, ValidationError) as exc:
                if corrected:
                    raise ProviderFailed(
                        "the model's answer was not valid JSON for the schema"
                    ) from exc
                corrected = True
                corrections = [
                    {"role": "assistant", "content": text[:4000] or "(empty)"},
                    {
                        "role": "user",
                        "content": "Your answer did not match the schema ("
                        + _short_error(exc)
                        + "). Answer again with one JSON object only.",
                    },
                ]
                continue
            break
        latency = round((time.monotonic() - started) * 1000)
        known = [c for c in costs if c is not None]
        cost = round(sum(known), 6) if costs and len(known) == len(costs) else None
        return answer, Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency,
            attempts=attempts,
            mode="json_schema" if use_schema else "prompt",
        )

    async def _call(
        self,
        completion: Completion,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None,
    ) -> tuple[Any, int]:
        module = self._module()
        exceptions = module.exceptions
        transient = (
            exceptions.Timeout,
            exceptions.APIConnectionError,
            exceptions.RateLimitError,
            exceptions.InternalServerError,
            exceptions.ServiceUnavailableError,
            exceptions.BadGatewayError,
        )
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout_seconds,
            "num_retries": 0,
            "drop_params": True,
        }
        if self.config.api_base:
            kwargs["api_base"] = self.config.api_base
        if self.config.api_key:
            kwargs["api_key"] = self.config.api_key
        if self.config.headers:
            kwargs["extra_headers"] = dict(self.config.headers)
        use_schema = response_format is not None
        if use_schema:
            kwargs["response_format"] = response_format
        for attempt in range(self.retries + 1):
            try:
                response = await completion(**kwargs)
            except transient as exc:
                if attempt >= self.retries:
                    raise ProviderFailed(
                        f"the model did not answer ({type(exc).__name__}) after "
                        f"{attempt + 1} attempts"
                    ) from exc
                _log.warning(
                    "vision model call failed, retrying",
                    model=self.config.model,
                    error=type(exc).__name__,
                    attempt=attempt + 1,
                )
                await self._sleep(min(8.0, 1.0 * 2**attempt))
                continue
            except exceptions.BadRequestError as exc:
                if use_schema:
                    _log.info(
                        "the model refused the JSON schema, asking in the prompt instead",
                        model=self.config.model,
                    )
                    raise _SchemaRefused from exc
                raise ProviderFailed(f"the model refused the request: {_safe(exc)}") from exc
            except (
                exceptions.AuthenticationError,
                exceptions.PermissionDeniedError,
                exceptions.NotFoundError,
                exceptions.ContentPolicyViolationError,
                exceptions.ContextWindowExceededError,
                exceptions.UnprocessableEntityError,
            ) as exc:
                raise ProviderFailed(
                    f"the model refused the request: {type(exc).__name__}"
                ) from exc
            except exceptions.APIError as exc:
                raise ProviderFailed(f"the model failed: {type(exc).__name__}") from exc
            return response, attempt + 1
        raise ProviderFailed("the model did not answer")  # pragma: no cover - loop returns

    def _cost(self, module: Any, response: Any) -> float | None:
        try:
            return float(module.completion_cost(completion_response=response))
        except Exception:
            return None


class _SchemaRefused(Exception):  # noqa: N818 - internal signal
    """The model answered 400 to the JSON schema."""


def _text_of(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError):
        return ""
    if isinstance(content, list):  # some families return content parts
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        location = ".".join(str(p) for p in first.get("loc", ()))
        return f"{location}: {first.get('msg', 'invalid')}"[:200]
    return str(exc)[:200]


def _safe(exc: Exception) -> str:
    """An error class name plus the HTTP status, never the message (it may echo input)."""
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}{f' ({status})' if status else ''}"
