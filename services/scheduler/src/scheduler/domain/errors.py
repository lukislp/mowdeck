"""Errors of the use cases. The API maps them to problem responses (``adapters.api.deps``)."""


class SchedulerError(Exception):
    """Base class; ``message`` is safe to show to the caller."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(SchedulerError):
    """The object does not exist in the caller's account or is not visible (404)."""


class ForbiddenError(SchedulerError):
    """The caller sees the object but may not do this (403)."""


class ConflictError(SchedulerError):
    """The change conflicts with the current state, e.g. a limit (409)."""


class InvalidInput(SchedulerError):  # noqa: N818 - reads like the HTTP status it maps to
    """The request is well-formed but not acceptable (422)."""


class Unavailable(SchedulerError):  # noqa: N818 - reads like the HTTP status it maps to
    """Fleet or the bus is not reachable; nothing was changed (503)."""
