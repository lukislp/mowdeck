"""Error lexicon lookups (ADR 0067): ``gateway.errors.explain``, ``gateway.errors.lexicons`` and
``gateway.errors.entries`` (a searchable page of one lexicon), core NATS request/reply.

The lexicons ship with the adapter packages installed in this image and register through the
entry point group ``mowdeck.error_lexicons`` (``mowdeck-control``); the gateway only answers with
them, it holds no code table of its own. Any instance may answer (queue group ``device-gateway``):
the lookups are not tied to a mower. Building a lexicon (Mammotion: about 660 codes) happens
once, at the first request, off the event loop.
"""

import asyncio

from mowdeck_contracts.v1 import (
    ErrorExplainReply,
    ErrorExplainRequest,
    ErrorLexiconListReply,
    ErrorLexiconSearchRequest,
)
from mowdeck_contracts.v1.subjects import (
    GATEWAY_ERRORS_ENTRIES,
    GATEWAY_ERRORS_EXPLAIN,
    GATEWAY_ERRORS_LEXICONS,
)
from mowdeck_control import ErrorLexiconRegistry, default_error_lexicons
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, Subscription
from pydantic import ValidationError

QUEUE = "device-gateway"


class ErrorLexiconResponder:
    """Answers lookups from an :class:`~mowdeck_control.ErrorLexiconRegistry`."""

    def __init__(self, registry: ErrorLexiconRegistry | None = None) -> None:
        self._registry = registry or default_error_lexicons()
        self._loaded = False
        self._lock = asyncio.Lock()

    async def _ready(self) -> ErrorLexiconRegistry:
        """The registry with its entry points loaded (in a thread, once)."""
        if not self._loaded:
            async with self._lock:
                if not self._loaded:
                    await asyncio.to_thread(self._registry.infos)
                    self._loaded = True
        return self._registry

    async def explain(self, message: BusMessage) -> None:
        """Decode an :class:`ErrorExplainRequest` and answer every code."""
        try:
            request = message.model(ErrorExplainRequest)
        except ValidationError as exc:
            raise DropMessage(f"invalid error explain request: {exc.error_count()} errors") from exc
        registry = await self._ready()
        explanations = registry.explain(
            request.codes,
            request.locale,
            vendor=request.vendor,
            connection=request.connection,
            category=request.category,
        )
        await message.respond(ErrorExplainReply(explanations=explanations))

    async def lexicons(self, message: BusMessage) -> None:
        """Answer the summaries of the installed lexicons."""
        registry = await self._ready()
        await message.respond(ErrorLexiconListReply(lexicons=registry.infos()))

    async def entries(self, message: BusMessage) -> None:
        """Decode an :class:`ErrorLexiconSearchRequest` and answer a page of the lexicon."""
        try:
            request = message.model(ErrorLexiconSearchRequest)
        except ValidationError as exc:
            raise DropMessage(f"invalid error lexicon search: {exc.error_count()} errors") from exc
        registry = await self._ready()
        await message.respond(registry.search(request))


async def subscribe_error_lexicons(
    bus: EventBus, registry: ErrorLexiconRegistry | None = None
) -> list[Subscription]:
    """Subscribe to the lookup subjects in the queue group ``device-gateway``."""
    responder = ErrorLexiconResponder(registry)
    return [
        await bus.subscribe(GATEWAY_ERRORS_EXPLAIN, responder.explain, queue=QUEUE),
        await bus.subscribe(GATEWAY_ERRORS_LEXICONS, responder.lexicons, queue=QUEUE),
        await bus.subscribe(GATEWAY_ERRORS_ENTRIES, responder.entries, queue=QUEUE),
    ]
