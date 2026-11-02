"""Helpers for the assistant tests (a plain module: the repository has no conftest.py).

``FakePlatform`` plays the public API (relations, mower, map, capabilities, state) and
``FakeProvider`` is the fake AI provider: it answers scripted JSON objects, including hostile
ones, and records what it was asked. No test calls a real AI API.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit.testing import StaticPrincipalResolver, lifespan_client, make_principal

from assistant.application.ports import (
    InterpreterAnswer,
    InterpreterKind,
    InterpreterStatus,
    MowerFacts,
)
from assistant.domain.catalog import build_catalog
from assistant.domain.errors import ProviderMissing
from assistant.domain.model import Area, Catalog
from assistant.main import build_app
from assistant.settings import Settings

API = "/api/v1/assistant"
NOW = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)  # 10:30 in Berlin, a Sunday
TZ = "Europe/Berlin"
TOKEN = "test-token"  # noqa: S105 - a placeholder the fakes compare, not a secret
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DEMO: dict[str, Any] = {"demo_mode": True, "demo_seed_token": "demo-seed-placeholder"}
"""Settings of demo mode (ADR 0068); the seed token is a test placeholder."""
AREAS = (Area("front", "Front lawn"), Area("back", "Back lawn"))
ALL_RELATIONS = {"can_view": True, "can_control": True, "can_edit_schedule": True}


def catalog(
    *,
    relations: dict[str, bool] | None = None,
    areas: tuple[Area, ...] = AREAS,
    activity: str | None = "docked",
    connected: bool = True,
    now: datetime = NOW,
) -> Catalog:
    """A catalog of a docked mower with two areas and every right (overridable)."""
    return build_catalog(
        mower_id=UUID(int=1),
        mower_name="Luba",
        now=now,
        timezone=TZ,
        relations=relations or ALL_RELATIONS,
        connected=connected,
        areas=areas,
        activity=activity,
    )


def action(
    kind: str,
    area_ids: Sequence[str] = (),
    start_at: str | None = None,
    duration_minutes: int | None = None,
) -> dict[str, Any]:
    """One action as a model writes it."""
    return {
        "kind": kind,
        "area_ids": list(area_ids),
        "start_at": start_at,
        "duration_minutes": duration_minutes,
    }


def answer(*actions: dict[str, Any], notes: Sequence[str] = ()) -> dict[str, Any]:
    """A model answer."""
    return {"actions": list(actions), "notes": list(notes)}


@dataclass
class FakePlatform:
    """The public API as the caller sees it."""

    relations_of: dict[str, bool] = field(default_factory=lambda: dict(ALL_RELATIONS))
    name: str = "Luba"
    area_names: dict[str, str] = field(default_factory=lambda: {"back": "Back lawn"})
    map: list[tuple[str, str | None]] = field(
        default_factory=lambda: [("front", "Front lawn"), ("back", "Garden B")]
    )
    is_connected: bool = True
    current: str | None = "docked"
    tokens: list[str] = field(default_factory=list)

    async def relations(
        self, token: str, mower_id: UUID, relations: Sequence[str]
    ) -> dict[str, bool]:
        self.tokens.append(token)
        return {r: self.relations_of.get(r, False) for r in relations}

    async def mower(self, token: str, mower_id: UUID) -> MowerFacts | None:
        self.tokens.append(token)
        if not self.relations_of.get("can_view"):
            return None
        return MowerFacts(self.name, self.area_names)

    async def map_areas(self, token: str, mower_id: UUID) -> list[tuple[str, str | None]]:
        return list(self.map)

    async def connected(self, token: str, mower_id: UUID) -> bool:
        return self.is_connected

    async def activity(self, token: str, mower_id: UUID) -> str | None:
        return self.current


@dataclass
class FakeProvider:
    """The fake AI provider: answers the scripted objects in turn (the last one repeats)."""

    answers: list[Any] = field(default_factory=lambda: [answer(action("dock"))])
    ready: bool = True
    missing: bool = False
    seen: list[tuple[str, Catalog, str]] = field(default_factory=list)

    @property
    def kind(self) -> InterpreterKind:
        return "provider"

    async def status(self, token: str) -> InterpreterStatus:
        if self.ready:
            return InterpreterStatus(ready=True, model="fake/assistant", hosted=False)
        return InterpreterStatus(ready=False, reason="no_provider")

    async def interpret(self, token: str, catalog: Catalog, text: str) -> InterpreterAnswer:
        if self.missing:
            raise ProviderMissing("no AI provider is available")
        self.seen.append((token, catalog, text))
        output = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return InterpreterAnswer(output, "fake/assistant")


def settings(**overrides: Any) -> Settings:
    """Settings for tests (environment independent)."""
    return Settings.model_validate({"environment": "test", "log_format": "console", **overrides})


@dataclass
class Harness:
    """The app under test with its fakes."""

    client: httpx.AsyncClient
    app: FastAPI
    platform: FakePlatform
    provider: FakeProvider
    principal: Principal
    mower_id: UUID

    async def ask(self, text: str, *, timezone: str = TZ) -> httpx.Response:
        """``POST /mowers/{id}/proposals``."""
        return await self.client.post(
            f"{API}/mowers/{self.mower_id}/proposals",
            json={"text": text, "timezone": timezone},
            headers=AUTH,
        )


@asynccontextmanager
async def harness(
    *,
    platform: FakePlatform | None = None,
    provider: FakeProvider | None = None,
    use_provider: bool = True,
    **overrides: Any,
) -> AsyncIterator[Harness]:
    """The app with the fakes, inside its lifespan."""
    fake_platform = platform or FakePlatform()
    fake_provider = provider or FakeProvider()
    principal = make_principal()
    app = build_app(
        settings(**overrides),
        platform=fake_platform,
        interpreter=fake_provider if use_provider else None,
        clock=lambda: NOW,
        principal_resolver=StaticPrincipalResolver(principal),
    )
    async with lifespan_client(app) as client:
        yield Harness(client, app, fake_platform, fake_provider, principal, uuid4())
