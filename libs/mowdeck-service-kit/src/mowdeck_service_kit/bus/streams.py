"""One-shot job that creates and updates the JetStream streams (ADR 0073).

    python -m mowdeck_service_kit.bus.streams

With per-service credentials no service may change a stream: only this job, as the NATS user
``streams``, creates the streams of :data:`mowdeck_contracts.v1.subjects.STREAMS` and updates
them to the contract settings; the services wait for them on connect. Compose runs it as the
service ``nats-streams`` before the services, Helm as a Job per release revision. Settings come
from the environment like a service's (``MOWDECK_NATS_URL``, ``MOWDECK_NATS_USER``,
``MOWDECK_NATS_PASSWORD[_FILE]``); the exit code is 0 when every stream exists.
"""

import asyncio
import sys

import nats.errors
from pydantic import ValidationError

from mowdeck_service_kit.bus.base import BusError
from mowdeck_service_kit.bus.jetstream import JetStreamBus
from mowdeck_service_kit.logging import configure_logging, get_logger
from mowdeck_service_kit.settings import ServiceSettings

JOB_NAME = "nats-streams"
"""Service name of the job in logs (the NATS connection name)."""

_log = get_logger(__name__)


async def ensure(settings: ServiceSettings, *, timeout_seconds: float = 60.0) -> None:
    """Connect as the job's user, create or update every stream, disconnect."""
    bus = JetStreamBus.from_settings(settings, manage_streams=True)
    async with asyncio.timeout(timeout_seconds):
        await bus.connect()
    try:
        _log.info("streams ensured", streams=[spec.name for spec in bus.streams])
    finally:
        await bus.close()


def main() -> int:
    """Entry point of ``python -m mowdeck_service_kit.bus.streams``."""
    try:
        settings = ServiceSettings(service_name=JOB_NAME)
    except ValidationError as exc:
        # Only the count: a message could quote a value, and one of them is a password.
        print(f"{JOB_NAME}: invalid settings: {exc.error_count()} errors", file=sys.stderr)
        return 2
    configure_logging(settings)
    try:
        asyncio.run(ensure(settings))
    except (TimeoutError, BusError, nats.errors.Error, OSError) as exc:
        _log.error("streams not ensured", error=repr(exc))
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
