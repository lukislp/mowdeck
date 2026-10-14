"""Entities of the Planner: what it knows about mowers (from the bus) and pattern templates."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class MowerFacts:
    """The newest map and capabilities of one mower (``device.mower.<id>.*`` snapshots)."""

    mower_id: UUID
    account_id: UUID
    map: dict[str, Any] | None = None
    """``MowerMap`` as JSON."""
    map_observed_at: datetime | None = None
    capabilities: dict[str, Any] | None = None
    """``MowerCapabilities`` as JSON."""
    capabilities_observed_at: datetime | None = None


@dataclass(slots=True)
class Template:
    """A saved pattern chain of an account, e.g. "2 border laps + diagonal + re-mow"."""

    id: UUID
    account_id: UUID
    name: str
    chain: list[dict[str, Any]]
    """``[{"pattern": key, "settings": {...}}, ...]``."""
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    description: str | None = None
