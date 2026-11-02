"""Errors raised by the domain and the use cases; the API maps them to problem responses."""


class IdentityError(Exception):
    """Base class. ``message`` is safe to show to the client."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidInput(IdentityError):  # noqa: N818 - reads like the HTTP status it maps to
    """The request violates a rule (422)."""


class NotFoundError(IdentityError):
    """The object does not exist or belongs to another account (404)."""


class ForbiddenError(IdentityError):
    """The actor is known but not allowed to do this (403)."""


class ConflictError(IdentityError):
    """The request conflicts with the current state (409), e.g. a taken username."""


class AuthenticationFailed(IdentityError):  # noqa: N818 - reads like the HTTP status it maps to
    """Credentials are wrong or unusable (401). The message is always generic."""

    def __init__(self, reason: str) -> None:
        super().__init__("invalid credentials")
        self.reason = reason
        """Internal reason for logs and the audit log; never shown to the client."""


class RateLimited(IdentityError):  # noqa: N818 - reads like the HTTP status it maps to
    """Too many attempts (429)."""

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class DemoFull(IdentityError):  # noqa: N818 - reads like the HTTP status it maps to
    """Every demo sandbox is taken (503, ADR 0076); try again after ``retry_after_seconds``."""

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AuthzUnavailable(IdentityError):  # noqa: N818 - reads like the HTTP status it maps to
    """OpenFGA could not be written; nothing was changed (503)."""
