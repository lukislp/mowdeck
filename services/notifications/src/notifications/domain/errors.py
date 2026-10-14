"""Errors of the use cases; the API maps them to problem responses."""

from datetime import timedelta


class NotificationsError(Exception):
    """Base class; ``message`` is safe to show to the caller."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AccountMismatchError(NotificationsError):
    """A message names another account than the mower directory."""


class NotFoundError(NotificationsError):
    """The resource does not exist for this user (or belongs to someone else)."""


class InvalidInputError(NotificationsError):
    """The request is well formed but not acceptable (422)."""


class RateLimitedError(NotificationsError):
    """Too many requests of this kind (429)."""

    def __init__(self, message: str, retry_after: timedelta) -> None:
        super().__init__(message)
        self.retry_after = retry_after
