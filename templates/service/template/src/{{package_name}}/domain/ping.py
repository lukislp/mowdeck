"""Example domain model, answered by the example endpoint."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Pong:
    """Answer of a ping: which service answered, when, and for which account."""

    service: str
    at: datetime
    account_id: UUID | None
