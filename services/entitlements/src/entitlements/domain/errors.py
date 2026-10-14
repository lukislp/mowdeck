"""Errors of the use cases. The API maps them to problem responses (``adapters.api.deps``)."""


class EntitlementsError(Exception):
    """Base class; ``message`` is safe to show to the caller."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(EntitlementsError):
    """The plan or account is unknown (404)."""


class InvalidInput(EntitlementsError):  # noqa: N818 - reads like the HTTP status it maps to
    """The request is well-formed but not acceptable (422)."""
