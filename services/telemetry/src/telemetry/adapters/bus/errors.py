"""Error lexicon lookups at the Device-Gateway (ADR 0067), core NATS request/reply:
``gateway.errors.explain``, ``gateway.errors.lexicons`` and ``gateway.errors.entries`` (queue group
``device-gateway``)."""

from collections.abc import Callable

from mowdeck_contracts.v1 import (
    ErrorExplainReply,
    ErrorExplainRequest,
    ErrorLexiconInfo,
    ErrorLexiconListReply,
    ErrorLexiconListRequest,
    ErrorLexiconSearchReply,
    ErrorLexiconSearchRequest,
)
from mowdeck_contracts.v1.subjects import (
    GATEWAY_ERRORS_ENTRIES,
    GATEWAY_ERRORS_EXPLAIN,
    GATEWAY_ERRORS_LEXICONS,
)
from mowdeck_service_kit.bus import EventBus


class BusErrorLexicons:
    """Implements :class:`~telemetry.application.errors.ErrorLexiconGateway`."""

    def __init__(self, bus: Callable[[], EventBus | None], *, timeout_seconds: float = 3.0) -> None:
        self._bus = bus
        self._timeout = timeout_seconds

    async def explain(self, request: ErrorExplainRequest) -> ErrorExplainReply | None:
        """Ask the gateway; None without a bus."""
        bus = self._bus()
        if bus is None:
            return None
        reply = await bus.request(GATEWAY_ERRORS_EXPLAIN, request, self._timeout)
        return reply.model(ErrorExplainReply)

    async def lexicons(self) -> list[ErrorLexiconInfo] | None:
        """The installed lexicons; None without a bus."""
        bus = self._bus()
        if bus is None:
            return None
        reply = await bus.request(GATEWAY_ERRORS_LEXICONS, ErrorLexiconListRequest(), self._timeout)
        return reply.model(ErrorLexiconListReply).lexicons

    async def search(self, request: ErrorLexiconSearchRequest) -> ErrorLexiconSearchReply | None:
        """A page of one lexicon; None without a bus."""
        bus = self._bus()
        if bus is None:
            return None
        reply = await bus.request(GATEWAY_ERRORS_ENTRIES, request, self._timeout)
        return reply.model(ErrorLexiconSearchReply)
