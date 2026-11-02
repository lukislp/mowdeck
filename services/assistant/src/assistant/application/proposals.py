"""The use cases: is the assistant ready for the caller, and a proposal for one request.

Nothing here executes anything. A proposal is the validated list of actions the web app shows
on a confirmation card; only after the user confirmed does the web app send them, one by one,
through Fleet and the Scheduler with the user's token (ADR 0075).
"""

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit import get_logger

from assistant.application.ports import (
    Clock,
    Interpreter,
    InterpreterKind,
    InterpreterStatus,
    Platform,
)
from assistant.domain.catalog import areas_of, build_catalog
from assistant.domain.errors import InvalidInput, NotFoundError, TooManyRequests
from assistant.domain.model import (
    CAN_CONTROL,
    CAN_EDIT_SCHEDULE,
    CAN_VIEW,
    Catalog,
    Interpretation,
)
from assistant.domain.schema import TEXT_MAX
from assistant.domain.validate import interpret

_log = get_logger("assistant.proposals")

PROPOSAL_TTL: Final = timedelta(minutes=5)
"""How long the web app may execute a proposal after it was made."""


@dataclass(frozen=True, slots=True)
class Proposal:
    """The answer to one request."""

    catalog: Catalog
    interpretation: Interpretation
    interpreter: InterpreterKind
    model: str | None
    created_at: datetime
    expires_at: datetime


@dataclass(slots=True)
class RateLimiter:
    """At most ``limit`` requests per subject in a sliding minute (in memory, per replica)."""

    limit: int
    window_seconds: float = 60.0
    _hits: dict[UUID, deque[float]] = field(default_factory=dict)

    def take(self, subject: UUID) -> bool:
        """Count one request; False when the subject is over its limit."""
        now = time.monotonic()
        hits = self._hits.setdefault(subject, deque())
        while hits and now - hits[0] > self.window_seconds:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > 10_000:  # forget idle subjects
            for key in [k for k, v in self._hits.items() if not v]:
                del self._hits[key]
        return True


class AssistantService:
    """Status and proposals."""

    def __init__(
        self,
        *,
        platform: Platform,
        interpreter: Interpreter,
        clock: Clock,
        limiter: RateLimiter,
    ) -> None:
        self.platform = platform
        self.interpreter = interpreter
        self.clock = clock
        self.limiter = limiter

    async def status(self, token: str) -> InterpreterStatus:
        """Whether the caller can ask now (a provider is configured, or the phrase parser)."""
        return await self.interpreter.status(token)

    async def propose(
        self, principal: Principal, token: str, mower_id: UUID, text: str, timezone: str
    ) -> Proposal:
        """Interpret ``text`` for one mower and validate the result (nothing is executed)."""
        request = text.strip()
        if not request or len(request) > TEXT_MAX:
            raise InvalidInput(f"text: 1 to {TEXT_MAX} characters")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise InvalidInput(f"unknown time zone {timezone[:64]!r}") from None
        if not self.limiter.take(principal.subject):
            raise TooManyRequests("too many requests: wait a minute")
        relations = await self.platform.relations(
            token, mower_id, (CAN_VIEW, CAN_CONTROL, CAN_EDIT_SCHEDULE)
        )
        if not relations.get(CAN_VIEW, False):
            raise NotFoundError("mower not found")
        facts = await self.platform.mower(token, mower_id)
        if facts is None:
            raise NotFoundError("mower not found")
        map_areas, connected, activity = await asyncio.gather(
            self.platform.map_areas(token, mower_id),
            self.platform.connected(token, mower_id),
            self.platform.activity(token, mower_id),
        )
        now = self.clock()
        catalog = build_catalog(
            mower_id=mower_id,
            mower_name=facts.name,
            now=now,
            timezone=timezone,
            relations=relations,
            connected=connected,
            areas=areas_of(map_areas, facts.area_names),
            activity=activity,
        )
        model: str | None = None
        if catalog.offered:
            answer = await self.interpreter.interpret(token, catalog, request)
            model = answer.model
            result = interpret(answer.output, catalog)
        else:
            result = Interpretation(())
        _log.info(
            "proposal",
            user_id=str(principal.subject),
            mower_id=str(mower_id),
            interpreter=self.interpreter.kind,
            offered=sorted(k.value for k in catalog.offered),
            actions=[a.kind.value for a in result.actions],
            rejected=[r.code.value for r in result.rejected],
            notes=[n.value for n in result.notes],
        )
        return Proposal(
            catalog=catalog,
            interpretation=result,
            interpreter=self.interpreter.kind,
            model=model,
            created_at=now,
            expires_at=now + PROPOSAL_TTL,
        )
