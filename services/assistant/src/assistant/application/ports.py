"""Ports: what the use cases need from the outside world, as protocols.

Every port acts with the caller's own access token against MowDeck's public API (ADR 0075):
the assistant has no credentials of its own, no NATS user and no database, so it can never see
or do more than the user could in the web app.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from assistant.domain.model import Catalog

InterpreterKind = Literal["provider", "phrases"]
"""``provider``: the user's AI provider through Vision; ``phrases``: the local phrase parser."""


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


@dataclass(frozen=True, slots=True)
class MowerFacts:
    """What Fleet says about the mower: its name and the custom names of its map areas."""

    name: str
    area_names: Mapping[str, str] = field(default_factory=dict)


class Platform(Protocol):
    """Reads of MowDeck's public API with the caller's token."""

    async def relations(
        self, token: str, mower_id: UUID, relations: Sequence[str]
    ) -> dict[str, bool]:
        """Identity's ``POST /me/permissions`` for the relations on ``mower:<id>``."""
        ...

    async def mower(self, token: str, mower_id: UUID) -> MowerFacts | None:
        """Fleet's ``GET /mowers/{id}``; None when the caller cannot see it."""
        ...

    async def map_areas(self, token: str, mower_id: UUID) -> list[tuple[str, str | None]]:
        """Telemetry's ``GET /mowers/{id}/map`` as ``(area id, own name)``; empty without
        a map."""
        ...

    async def connected(self, token: str, mower_id: UUID) -> bool:
        """Whether the mower ever reported its capabilities (Telemetry's
        ``GET /mowers/{id}/capabilities``)."""
        ...

    async def activity(self, token: str, mower_id: UUID) -> str | None:
        """The mower's current activity (Telemetry's ``GET /mowers/{id}/state``); None when
        unknown."""
        ...


@dataclass(frozen=True, slots=True)
class InterpreterStatus:
    """Whether requests can be interpreted now, and why not."""

    ready: bool
    reason: Literal["no_provider", "provider_unavailable"] | None = None
    model: str | None = None
    hosted: bool | None = None


@dataclass(frozen=True, slots=True)
class InterpreterAnswer:
    """The raw answer (untrusted, validated by the caller) and the model that gave it."""

    output: Any
    model: str | None = None


class Interpreter(Protocol):
    """Turns a request into an answer shaped like :func:`assistant.domain.schema.answer_schema`."""

    @property
    def kind(self) -> InterpreterKind:
        """Which interpreter this is."""
        ...

    async def status(self, token: str) -> InterpreterStatus:
        """Whether the caller can use it now."""
        ...

    async def interpret(self, token: str, catalog: Catalog, text: str) -> InterpreterAnswer:
        """The answer for ``text`` (raises the provider errors of the domain)."""
        ...
