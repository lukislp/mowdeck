"""The two interpreters: the user's AI provider (through Vision) and the phrase parser.

``ProviderInterpreter`` reuses the AI provider the user configured under Settings, AI provider
(ADR 0033) through Vision's ``POST /me/provider/structured`` with the user's token: Vision
resolves the user's own provider or the installation's, applies its limits and cost accounting
and never hands out a key. ``PhraseInterpreter`` calls no AI at all (demo mode, tests).
"""

from typing import Any

import httpx

from assistant.application.ports import (
    InterpreterAnswer,
    InterpreterKind,
    InterpreterStatus,
)
from assistant.domain.errors import (
    ProviderFailed,
    ProviderMissing,
    ProviderQuota,
    UpstreamUnavailable,
)
from assistant.domain.model import Catalog
from assistant.domain.phrases import parse
from assistant.domain.schema import SCHEMA_NAME, SYSTEM_PROMPT, answer_schema, user_message

PURPOSE = "assistant"
"""How the calls appear in Vision's cost accounting."""


class ProviderInterpreter:
    """``Interpreter`` on the caller's AI provider (Vision's structured answers)."""

    def __init__(self, client: httpx.AsyncClient, *, vision_url: str, timeout: float) -> None:
        self.client = client
        self.base = vision_url.rstrip("/") + "/api/v1/vision"
        self.timeout = timeout

    @property
    def kind(self) -> InterpreterKind:
        return "provider"

    async def status(self, token: str) -> InterpreterStatus:
        try:
            response = await self.client.get(f"{self.base}/me/provider", headers=_auth(token))
        except httpx.HTTPError:
            return InterpreterStatus(ready=False, reason="provider_unavailable")
        if response.status_code != 200:
            return InterpreterStatus(ready=False, reason="provider_unavailable")
        body: dict[str, Any] = response.json()
        effective = body.get("effective")
        if effective == "own":
            return InterpreterStatus(ready=True, model=_str(body.get("model")), hosted=False)
        if effective == "hosted":
            return InterpreterStatus(ready=True, model=_str(body.get("hosted_model")), hosted=True)
        return InterpreterStatus(ready=False, reason="no_provider")

    async def interpret(self, token: str, catalog: Catalog, text: str) -> InterpreterAnswer:
        payload = {
            "purpose": PURPOSE,
            "name": SCHEMA_NAME,
            "system": SYSTEM_PROMPT,
            "text": user_message(catalog, text),
            "schema": answer_schema(catalog),
        }
        try:
            response = await self.client.post(
                f"{self.base}/me/provider/structured",
                json=payload,
                headers=_auth(token),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable("vision did not answer") from exc
        match response.status_code:
            case 200:
                body: dict[str, Any] = response.json()
                return InterpreterAnswer(body.get("output"), _str(body.get("model")))
            case 409:
                raise ProviderMissing(
                    "no AI provider is available: configure one under Settings, AI provider"
                )
            case 429:
                raise ProviderQuota("the AI provider's hourly limit is reached")
            case 502:
                raise ProviderFailed("the AI model gave no usable answer")
            case 404:
                raise UpstreamUnavailable("the AI provider settings (vision) are not deployed")
            case status:
                raise UpstreamUnavailable(f"vision answered {status}")


class PhraseInterpreter:
    """``Interpreter`` without any AI: a few German and English phrases (demo mode, tests)."""

    @property
    def kind(self) -> InterpreterKind:
        return "phrases"

    async def status(self, token: str) -> InterpreterStatus:
        return InterpreterStatus(ready=True)

    async def interpret(self, token: str, catalog: Catalog, text: str) -> InterpreterAnswer:
        return InterpreterAnswer(parse(text, catalog))


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _str(value: object) -> str | None:
    return value[:200] if isinstance(value, str) else None
