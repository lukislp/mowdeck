"""Vendor error codes in plain words (ADR 0067).

Telemetry holds no code table: the lexicons ship with the adapter packages, which only the
Device-Gateway installs, so every lookup asks the gateway (:class:`ErrorLexiconGateway`, core
NATS ``gateway.errors.explain``). Answers are cached per (vendor or connection, code, locale,
category) for ``cache_seconds``; a gateway that does not answer gives the generic explanation
of :func:`~mowdeck_contracts.v1.unknown_explanation` (``known`` false), which is not cached, so
the next request asks again.
"""

import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mowdeck_contracts.v1 import (
    ConnectionKind,
    ErrorExplainReply,
    ErrorExplainRequest,
    ErrorExplanation,
    ErrorLexiconInfo,
    MowerActivity,
    MowerErrorCategory,
    normalise_locale,
    unknown_explanation,
)
from mowdeck_service_kit import get_logger

from telemetry.application.queries import Queries

_log = get_logger("telemetry.errors")

CacheKey = tuple[str | None, str | None, str, str, str | None]


class ErrorLexiconGateway(Protocol):
    """Asks the Device-Gateway; None without a bus, raises when it does not answer."""

    async def explain(self, request: ErrorExplainRequest) -> ErrorExplainReply | None:
        """Explain the codes of ``request``."""
        ...

    async def lexicons(self) -> list[ErrorLexiconInfo] | None:
        """The installed lexicons."""
        ...


@dataclass(frozen=True, slots=True)
class MowerError:
    """The current error of a mower, explained (``explanation`` None: no error)."""

    mower_id: UUID
    observed_at: datetime
    activity: str
    explanation: ErrorExplanation | None


def _category(value: object) -> MowerErrorCategory | None:
    try:
        return MowerErrorCategory(str(value)) if value else None
    except ValueError:
        return None


def _connection(value: object) -> ConnectionKind | None:
    try:
        return ConnectionKind(str(value)) if value else None
    except ValueError:
        return None


class ErrorExplanations:
    """Explains codes through the gateway, with a small cache and the generic fallback."""

    def __init__(
        self,
        gateway: ErrorLexiconGateway,
        queries: Queries,
        *,
        cache_seconds: float = 3600.0,
        cache_size: int = 2048,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gateway = gateway
        self._queries = queries
        self._ttl = cache_seconds
        self._size = cache_size
        self._clock = clock
        self._cache: OrderedDict[CacheKey, tuple[float, ErrorExplanation]] = OrderedDict()

    def _cached(self, key: CacheKey) -> ErrorExplanation | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        if self._clock() - hit[0] > self._ttl:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return hit[1]

    def _store(self, key: CacheKey, explanation: ErrorExplanation) -> None:
        self._cache[key] = (self._clock(), explanation)
        self._cache.move_to_end(key)
        while len(self._cache) > self._size:
            self._cache.popitem(last=False)

    async def explain(
        self,
        codes: Sequence[str],
        locale: str | None = None,
        *,
        vendor: str | None = None,
        connection: ConnectionKind | None = None,
        category: MowerErrorCategory | None = None,
    ) -> list[ErrorExplanation]:
        """One explanation per code, in order; never raises."""
        lang = normalise_locale(locale)
        conn = connection.value if connection is not None else None
        cat = category.value if category is not None else None
        keys = [(vendor, None if vendor else conn, code, lang, cat) for code in codes]
        found = {key: hit for key in keys if (hit := self._cached(key)) is not None}
        missing = list(dict.fromkeys(key[2] for key in keys if key not in found))
        if missing:
            reply: ErrorExplainReply | None = None
            try:
                reply = await self._gateway.explain(
                    ErrorExplainRequest(
                        codes=missing,
                        vendor=vendor,
                        connection=None if vendor else connection,
                        locale=lang,
                        category=category,
                    )
                )
            except Exception as exc:
                _log.warning("error lexicon lookup failed", error=repr(exc), codes=len(missing))
            if reply is not None and len(reply.explanations) == len(missing):
                for code, explanation in zip(missing, reply.explanations, strict=True):
                    key = (vendor, None if vendor else conn, code, lang, cat)
                    self._store(key, explanation)
                    found[key] = explanation
        return [
            found.get(key) or unknown_explanation(key[2], lang, vendor=vendor, category=category)
            for key in keys
        ]

    async def explain_for_mower(
        self, account_id: UUID, mower_id: UUID, codes: Sequence[str], locale: str | None
    ) -> list[ErrorExplanation]:
        """Explain ``codes`` with the lexicon of the mower's connection (its latest state)."""
        row = await self._queries.state(account_id, mower_id)
        return await self.explain(
            codes, locale, connection=_connection(row.state.get("connection"))
        )

    async def current(self, account_id: UUID, mower_id: UUID, locale: str | None) -> MowerError:
        """The mower's current error explained; ``explanation`` None while it reports none.

        An error code outside the activity ``error`` (a paused mower with a fault of its window)
        counts too: the state carries it because it holds the mower.
        """
        row = await self._queries.state(account_id, mower_id)
        state = row.state
        code = state.get("error_code")
        activity = str(state.get("activity") or MowerActivity.UNKNOWN)
        explanation: ErrorExplanation | None = None
        if code:
            [explanation] = await self.explain(
                [str(code)],
                locale,
                connection=_connection(state.get("connection")),
                category=_category(state.get("error_category")),
            )
        return MowerError(
            mower_id=mower_id,
            observed_at=row.observed_at,
            activity=activity,
            explanation=explanation,
        )

    async def lexicons(self) -> list[ErrorLexiconInfo]:
        """The installed lexicons; empty when the gateway does not answer."""
        try:
            return await self._gateway.lexicons() or []
        except Exception as exc:
            _log.warning("error lexicon list failed", error=repr(exc))
            return []
