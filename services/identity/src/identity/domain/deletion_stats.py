# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account deletions per month for the operator's payments dashboard (ADR 0083), free of
storage. Counts only: no account id, no name.

* **scheduled**: an owner requested the deletion of the account (the audit action
  ``account.deletion_requested``; audit rows stay, scrubbed, after an erasure).
* **restored**: the owner took a request back within the grace period (``account.restored``).
* **completed**: the erasure finished, every service confirmed (``AccountErasure.completed_at``).

Months are calendar months in UTC (``YYYY-MM``), the same buckets as Billing's statistics; a
window of ``n`` months ends with the month of ``now``.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

DELETION_REQUESTED = "account.deletion_requested"
ACCOUNT_RESTORED = "account.restored"


def month_of(at: datetime) -> str:
    """The month bucket of an instant (``YYYY-MM`` in UTC)."""
    return at.astimezone(UTC).strftime("%Y-%m")


def recent_months(now: datetime, count: int) -> list[str]:
    """The ``count`` months up to and including the month of ``now``, oldest first."""
    local = now.astimezone(UTC)
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
) -> list[DeletionMonth]:
    """Every month of the window, oldest first, with how many deletions were scheduled,
    restored and completed in it."""
    window = recent_months(now, months)
    counts = [Counter(month_of(at) for at in times) for times in (scheduled, restored, completed)]
    return [DeletionMonth(m, counts[0][m], counts[1][m], counts[2][m]) for m in window]
