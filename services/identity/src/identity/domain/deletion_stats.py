# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account deletions per month for the operator's payments dashboard (ADR 0083), free of
storage. Counts only: no account id, no name.

* **scheduled**: an owner requested the deletion of the account (the audit action
  ``account.deletion_requested``; audit rows stay, scrubbed, after an erasure).
* **restored**: the owner took a request back within the grace period (``account.restored``).
* **completed**: the erasure finished, every service confirmed (``AccountErasure.completed_at``).

Months are local calendar months (``YYYY-MM``) in the zone the caller names: the web app passes
the ``time_zone`` of Billing's dashboard (``MOWDECK_BILLING_REPORT_TIMEZONE``), so both services
count the same months; ``zoneinfo`` keeps the buckets right across daylight saving time. A
window of ``n`` months ends with the month of ``now``.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from identity.domain.errors import InvalidInput

DELETION_REQUESTED = "account.deletion_requested"
ACCOUNT_RESTORED = "account.restored"

DEFAULT_ZONE = "Europe/Berlin"
"""The zone when the caller names none (Billing's default report zone)."""


def report_zone(name: str) -> ZoneInfo:
    """The IANA zone ``name``; ``InvalidInput`` (422) for an unknown one."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        msg = f"unknown IANA time zone {name!r}"
        raise InvalidInput(msg) from exc


def month_of(at: datetime, zone: tzinfo) -> str:
    """The month bucket of an instant: ``YYYY-MM`` of its local date in ``zone``."""
    return at.astimezone(zone).strftime("%Y-%m")


def recent_months(now: datetime, count: int, zone: tzinfo) -> list[str]:
    """The ``count`` months up to and including the local month of ``now``, oldest first."""
    local = now.astimezone(zone)
    index = local.year * 12 + local.month - 1
    return [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(index - count + 1, index + 1)]


@dataclass(frozen=True, slots=True)
class DeletionMonth:
    """The account deletions of one month."""

    month: str
    scheduled: int
    restored: int
    completed: int


def deletions_by_month(
    *,
    scheduled: Iterable[datetime],
    restored: Iterable[datetime],
    completed: Iterable[datetime],
    now: datetime,
    months: int,
    zone: tzinfo,
) -> list[DeletionMonth]:
    """Every month of the window, oldest first, with how many deletions were scheduled,
    restored and completed in it."""
    window = recent_months(now, months, zone)
    counts = [
        Counter(month_of(at, zone) for at in times) for times in (scheduled, restored, completed)
    ]
    return [DeletionMonth(m, counts[0][m], counts[1][m], counts[2][m]) for m in window]
