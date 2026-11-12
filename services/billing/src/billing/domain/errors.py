# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

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


class ProviderUnavailable(ProviderError):  # noqa: N818 - reads like the state it reports
    """The payment provider could not be reached or asked to slow down (a rate limit, a timeout,
    an error on its side): the same call can succeed later."""


class RefundImpossible(ProviderError):  # noqa: N818 - reads like the state it reports
    """The provider refuses a refund for a reason that does not go away by asking again: the
    payment is older than the provider allows, it is disputed, or its payment method cannot be
    refunded. The account erasure skips such a refund and warns the operator (ADR 0082)."""


class RefundFailed(ProviderError):  # noqa: N818 - reads like the state it reports
    """A refund the provider reports as failed or canceled: the money did not go back. The
    account erasure fails and tries again (ADR 0082)."""
