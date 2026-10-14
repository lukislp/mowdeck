"""Exceptions of the control library.

Adapters raise :class:`CommandRejected` when the mower refuses a command before doing anything
(wrong state, device limits) and :class:`NotConnected` when there is no link. Every other
exception from an adapter is reported by the controller as a failed command.
"""


class ControlError(Exception):
    """Base class of all control library errors."""


class NotConnected(ControlError):  # noqa: N818 - outcome names read best
    """The connection to the mower is not established."""


class CommandRejected(ControlError):  # noqa: N818 - outcome names read best
    """The mower or the adapter refused a command before executing it.

    ``reason`` is shown to the user, so it must not contain secrets or internals.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SensorStop(CommandRejected):
    """A safety sensor (lift, tilt, collision) stopped the mower. It cannot be overridden."""


class UnsupportedPattern(ControlError):  # noqa: N818 - outcome names read best
    """The requested mowing pattern cannot run natively on this mower."""


class UnknownConnectionKind(ControlError, LookupError):  # noqa: N818 - outcome names read best
    """No connection factory is registered for the requested kind."""


class PathUnavailable(ControlError):  # noqa: N818 - outcome names read best
    """The mower did not hand back a planned mow path (no answer in time, or an empty one)."""

    def __init__(self, reason: str, *, timed_out: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.timed_out = timed_out
