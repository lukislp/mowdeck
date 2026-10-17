"""Relay tickets: what a WHEP bearer token allows (the scope; signing is an adapter).

A ticket is bound to one account, one mower, one source session (its random ``relay_id``), one
lease and the media paths of that lease's cameras, and lives ``RELAY_TICKET_SECONDS``. Paths are
``r/<relay_id>/<camera>``: random per session, never the mower id.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PATH_PREFIX = "r"


class InvalidTicketError(Exception):
    """A ticket that is malformed, wrongly signed or expired."""


def camera_segment(camera_id: str) -> str:
    """The path segment of a camera: the id when it is path-safe, else a stable hash of it."""
    if _SAFE_SEGMENT.match(camera_id):
        return camera_id
    return "x" + hashlib.sha256(camera_id.encode("utf-8")).hexdigest()[:24]


def camera_path(relay_id: str, camera_id: str) -> str:
    """The media path of one camera of a session: ``r/<relay_id>/<camera>``."""
    return f"{PATH_PREFIX}/{relay_id}/{camera_segment(camera_id)}"


@dataclass(frozen=True, slots=True)
class TicketClaims:
    """The scope of one ticket."""

    account_id: UUID
    mower_id: UUID
    relay_id: str
    lease_id: UUID
    paths: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime

    def allows(self, *, account_id: UUID, path: str, now: datetime) -> bool:
        """True when this ticket opens ``path`` for ``account_id`` at ``now``."""
        return account_id == self.account_id and path in self.paths and now < self.expires_at
