# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Ports: what the use cases need from the outside world, as protocols."""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...
