"""Rules of the Device-Gateway as pure functions."""

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from mowdeck_contracts.v1 import (
    CameraSessionErrorCode,
    CameraSessionRequest,
    CommandEnvelope,
    MowerRegistration,
    MowerState,
)


@dataclass(frozen=True, slots=True)
class Shard:
    """The part of all mowers one instance is responsible for.

    A mower belongs to shard ``index`` of ``count`` when ``mower_id.int % count == index``.
    Changing ``count`` moves mowers between instances (they reconnect on the new owner).
    """

    index: int = 0
    count: int = 1

    def __post_init__(self) -> None:
        if self.count < 1 or not 0 <= self.index < self.count:
            msg = f"invalid shard {self.index} of {self.count}"
            raise ValueError(msg)

    def owns(self, mower_id: UUID) -> bool:
        """True if this shard holds the connection of ``mower_id``."""
        return mower_id.int % self.count == self.index


class ConnectionStatus(StrEnum):
    """Link state of one mower held by the gateway."""

    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"


def command_rejection(
    registration: MowerRegistration | None, envelope: CommandEnvelope
) -> str | None:
    """Why ``envelope`` must not reach the mower, or None if it may.

    The account check is defence in depth: Fleet already checked the permission, but a command
    whose account differs from the mower's registered owner never reaches the controller.
    """
    if registration is None:
        return "unknown mower"
    if registration.mower_id != envelope.mower_id:
        return "command is for another mower"
    if registration.account_id != envelope.account_id:
        return "the command's account does not own this mower"
    return None


def camera_rejection(
    registration: MowerRegistration | None, request: CameraSessionRequest
) -> tuple[CameraSessionErrorCode, str] | None:
    """Why a camera session must not be given, or None if it may (the command account rule)."""
    if registration is None:
        return CameraSessionErrorCode.NOT_FOUND, "unknown mower"
    if registration.account_id != request.account_id:
        return CameraSessionErrorCode.FORBIDDEN, "the request's account does not own this mower"
    return None


def is_significant_change(previous: MowerState | None, current: MowerState) -> bool:
    """True if ``current`` differs from ``previous`` beyond the timestamp and adapter extras.

    ``observed_at`` changes with every snapshot and ``raw`` holds adapter specific counters, so
    neither counts as a change; the heartbeat publishes them regularly anyway.
    """
    if previous is None:
        return True
    ignored = {"observed_at", "raw"}
    return previous.model_dump(exclude=ignored) != current.model_dump(exclude=ignored)


def backoff(initial: float, maximum: float, factor: float = 2.0) -> Iterator[float]:
    """Delays ``initial, initial*factor, ...`` capped at ``maximum``, forever."""
    if initial <= 0 or maximum < initial or factor < 1:
        msg = "backoff needs 0 < initial <= maximum and factor >= 1"
        raise ValueError(msg)
    delay = initial
    while True:
        yield delay
        delay = min(delay * factor, maximum)
