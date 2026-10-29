"""Explains vendor error codes for the push texts (ADR 0067).

Core NATS request/reply on ``gateway.errors.explain`` (queue group ``device-gateway``): the
Device-Gateway answers from the error lexicons of the installed adapter packages, picking the
lexicon by the mower's cloud provider (``MowerState.cloud_provider``), else by its connection
kind. Answers are kept for an hour per (provider, connection, code, language); a gateway that
does not answer gives None, so the push keeps the vendor's own message.
"""

import time
from collections.abc import Callable

from mowdeck_contracts.v1 import (
    ConnectionKind,
    ErrorExplainReply,
    ErrorExplainRequest,
    ErrorExplanation,
    MowerErrorCategory,
)
from mowdeck_contracts.v1.subjects import GATEWAY_ERRORS_EXPLAIN
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusError, EventBus
from pydantic import ValidationError

_log = get_logger("notifications.errors")

CACHE_SECONDS = 3600.0
CACHE_SIZE = 512


class BusErrorExplainer:
    """``ErrorExplainer`` over ``gateway.errors.explain``."""

    def __init__(
        self,
        bus: EventBus,
        *,
        timeout_seconds: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._timeout = timeout_seconds
        self._clock = clock
        self._cache: dict[tuple[str, str, str, str], tuple[float, ErrorExplanation]] = {}

    async def explain(
        self,
        code: str,
        connection: str,
        language: str,
        category: str | None = None,
        cloud_provider: str | None = None,
    ) -> ErrorExplanation | None:
        """The explanation of ``code`` for a mower of ``cloud_provider`` (else on
        ``connection``); None when unavailable."""
        key = (cloud_provider or "", connection, code, language)
        hit = self._cache.get(key)
        if hit is not None and self._clock() - hit[0] <= CACHE_SECONDS:
            return hit[1]
        if not self._bus.is_connected:
            return None
        try:
            request = ErrorExplainRequest(
                codes=[code],
                cloud_provider=cloud_provider or None,
                connection=ConnectionKind(connection) if connection else None,
                locale=language,
                category=MowerErrorCategory(category) if category else None,
            )
            message = await self._bus.request(GATEWAY_ERRORS_EXPLAIN, request, self._timeout)
            [explanation] = message.model(ErrorExplainReply).explanations
        except (BusError, ValidationError, ValueError) as exc:
            _log.info("no explanation for an error code", code=code, error=type(exc).__name__)
            return None
        if len(self._cache) >= CACHE_SIZE:
            self._cache.clear()
        self._cache[key] = (self._clock(), explanation)
        return explanation
