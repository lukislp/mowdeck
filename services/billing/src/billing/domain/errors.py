"""Errors of the billing use cases (mapped to problem details in the API adapter)."""


class BillingError(Exception):
    """Base class; ``message`` is safe to show to the caller."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(BillingError):
    """The thing asked for does not exist (404)."""


class InvalidInput(BillingError):  # noqa: N818 - reads like the HTTP status it maps to
    """The request cannot be served as asked (422)."""


class ConflictError(BillingError):
    """The request conflicts with the current state, e.g. a subscription exists (409)."""


class InvalidSignature(BillingError):  # noqa: N818 - reads like the HTTP status it maps to
    """A webhook whose signature does not verify (400)."""


class ProviderError(BillingError):
    """The payment provider failed or is not configured (503)."""
