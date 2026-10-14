"""Configuration of the use cases (built from ``Settings`` in ``main``)."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """The settings the use cases need."""

    batch_size: int = 50
    """Schedules one tick fires at most (and open runs it follows)."""
    max_permission_skips: int = 3
    """Firings in a row skipped for missing ``can_control`` before the schedule is disabled."""
    max_schedules_per_account: int = 100
    run_retention: timedelta = timedelta(days=90)
    finished_grace: timedelta = timedelta(minutes=2)
    """A state at least this long after the start that shows the mower docked ends the job."""
    followup_horizon: timedelta = timedelta(hours=2)
    """Open runs are closed this long after their window ended."""
    preview_max: int = 20
