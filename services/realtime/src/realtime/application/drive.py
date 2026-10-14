"""Manual driving over the socket (ADR 0026): the state of one drive session and its frames.

Realtime adds no safety rule of its own. The Device-Gateway's controller is the only safety
authority: each frame is a ``ManualDrive`` command whose ``ttl_ms`` re-arms the dead-man timer,
and the blade rules apply to every frame. Realtime only decides who may open a session (with
Fleet), limits the frame rate, and stops sending frames when the session ends, so the mower
stops within ``ttl_ms`` at the latest.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from mowdeck_contracts.v1 import (
    CommandEnvelope,
    ManualDrive,
    PrincipalRef,
    drive_correlation_id,
)
from mowdeck_contracts.v1.realtime import DriveCapabilities


class TokenBucket:
    """Allows ``rate`` frames per second on average with a small burst for network jitter."""

    def __init__(self, rate: float, burst: float, now: float) -> None:
        if rate <= 0 or burst < 1:
            msg = "rate must be positive and burst at least 1"
            raise ValueError(msg)
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.updated = now

    def allow(self, now: float) -> bool:
        """Take one token at monotonic time ``now``; False when none is left."""
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


@dataclass(eq=False)
class ActiveDrive:
    """An open drive session of one socket."""

    session_id: UUID
    mower_id: UUID
    account_id: UUID
    """The mower's account, from Fleet's reply (the envelopes' ``account_id``)."""
    blade_on_allowed: bool
    capabilities: DriveCapabilities
    started_at: datetime
    limiter: TokenBucket
    frames: int = 0
    dropped: int = 0
    last_frame_at: float | None = None
    """Loop time of the last published frame (the stop frame is only needed while it lives)."""
    timer: asyncio.Task[None] | None = field(default=None, repr=False)


def frame_envelope(
    drive: ActiveDrive,
    issued_by: PrincipalRef,
    *,
    linear: float,
    angular: float,
    blade_on: bool,
    ttl_ms: int,
    now: datetime,
) -> CommandEnvelope:
    """The command for one frame: expires with its dead-man time, names the session.

    ``blade_on`` is forced off unless the session allows the blades (the controller applies
    the blade rules again).
    """
    return CommandEnvelope.create(
        mower_id=drive.mower_id,
        account_id=drive.account_id,
        issued_by=issued_by,
        command=ManualDrive(
            linear=linear,
            angular=angular,
            blade_on=blade_on and drive.blade_on_allowed,
            ttl_ms=ttl_ms,
        ),
        ttl=timedelta(milliseconds=ttl_ms),
        correlation_id=drive_correlation_id(drive.session_id),
        now=now,
    )
