"""Errors of the use cases. The API maps them to problem responses (``adapters.api.deps``)."""

from collections.abc import Sequence


class PlannerError(Exception):
    """Base class; ``message`` is safe to show to the caller."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(PlannerError):
    """The object does not exist in the caller's account or is not visible (404)."""


class ForbiddenError(PlannerError):
    """The caller sees the object but may not do this (403)."""


class ConflictError(PlannerError):
    """The change conflicts with the current state, e.g. a limit (409)."""


class InvalidInput(PlannerError):  # noqa: N818 - reads like the HTTP status it maps to
    """The request is well-formed but not acceptable (422); ``errors`` lists details."""

    def __init__(self, message: str, errors: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.errors = tuple(errors)


class Unavailable(PlannerError):  # noqa: N818 - reads like the HTTP status it maps to
    """Telemetry or the bus is not reachable (503)."""
