"""Seeds the mower directory from Fleet (``fleet.mowers.list``, core NATS request/reply).

The durable consumers start at new messages, so a Telemetry deployed after Fleet would miss the
earlier ``mower.registered`` events. One request with ``shard_index=0, shard_count=1`` lists
every mower; it is retried with a growing delay while Fleet is absent (not fatal: the events
fill the directory in the meantime).
"""

import asyncio
from collections.abc import Callable, Iterator

from mowdeck_contracts.v1 import MowerListReply, MowerListRequest
from mowdeck_contracts.v1.subjects import FLEET_MOWERS_LIST
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import EventBus

from telemetry.application.ingest import Ingest

_log = get_logger("telemetry.fleet")


def backoff(first: float, maximum: float) -> Iterator[float]:
    """1, 2, 4, ... seconds up to ``maximum``, forever."""
    delay = first
    while True:
        yield min(delay, maximum)
        delay *= 2


async def seed_directory(
    bus: EventBus,
    ingest: Ingest,
    *,
    timeout_seconds: float,
    retry_max_seconds: float,
    on_seeded: Callable[[int], None] | None = None,
    first_delay: float = 1.0,
) -> int:
    """Ask Fleet for all mowers until it answers; returns how many were listed."""
    for attempt, delay in enumerate(backoff(first_delay, retry_max_seconds), start=1):
        try:
            reply = await bus.request(
                FLEET_MOWERS_LIST, MowerListRequest(shard_index=0, shard_count=1), timeout_seconds
            )
            count = await ingest.seed(reply.model(MowerListReply).mowers)
        except Exception as exc:
            _log.warning(
                "fleet did not answer fleet.mowers.list, the events fill the directory meanwhile",
                attempt=attempt,
                retry_in_seconds=delay,
                error=repr(exc),
            )
            await asyncio.sleep(delay)
            continue
        _log.info("mower directory seeded from fleet", count=count)
        if on_seeded is not None:
            on_seeded(count)
        return count
    raise AssertionError("unreachable")  # pragma: no cover - backoff() never ends
