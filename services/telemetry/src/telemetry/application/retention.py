"""Retention: delete history, positions and command timeline older than their limits.

Only one replica deletes at a time: the store hands out a scope under
``pg_try_advisory_lock``; a replica that does not get it does nothing (the in-memory store has
no lock and always works).
"""

from mowdeck_service_kit import get_logger

from telemetry.application.context import TelemetryConfig
from telemetry.application.ports import Clock, RetentionTarget, Store

_log = get_logger("telemetry.retention")


class Retention:
    """The periodic cleanup."""

    def __init__(self, store: Store, config: TelemetryConfig, clock: Clock) -> None:
        self._store = store
        self._config = config
        self._clock = clock

    async def run_once(self) -> dict[RetentionTarget, int] | None:
        """Delete expired rows; None when another replica holds the lock."""
        limits = {
            RetentionTarget.HISTORY: self._config.retention_history,
            RetentionTarget.POSITIONS: self._config.retention_positions,
            RetentionTarget.COMMANDS: self._config.retention_commands,
        }
        async with self._store.retention() as scope:
            if scope is None:
                _log.debug("retention skipped, another replica holds the lock")
                return None
            now = self._clock()
            deleted = {
                target: await scope.delete_before(
                    target, now - limit, self._config.retention_batch_size
                )
                for target, limit in limits.items()
            }
        return deleted
