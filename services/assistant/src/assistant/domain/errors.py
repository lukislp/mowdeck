# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Errors of the assistant's use cases; the API maps each to a problem type."""


class AssistantError(Exception):
    """Base of the use case errors (``message`` is safe to show)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AssistantError):
    """The mower does not exist or the caller cannot see it (404)."""


class InvalidInput(AssistantError):  # noqa: N818 - reads as a state
    """A field of the request is out of its limits (422)."""


class ProviderMissing(AssistantError):  # noqa: N818 - reads as a state
    """No AI provider may be used: the user has to configure one (409)."""


class ConsentRequired(AssistantError):  # noqa: N818 - reads as a state
    """The AI provider is a cloud model the caller has not agreed to (403). Vision owns the
    consent rules; the assistant only passes the refusal on so the app can ask."""


class ProviderQuota(AssistantError):  # noqa: N818 - reads as a state
    """The provider's hourly limit is reached (429)."""


class ProviderFailed(AssistantError):  # noqa: N818 - reads as a state
    """The model gave no usable answer (502)."""


class TooManyRequests(AssistantError):  # noqa: N818 - reads as a state
    """The caller asked too often in a short time (429)."""


class UpstreamUnavailable(AssistantError):  # noqa: N818 - reads as a state
    """A MowDeck service the assistant reads from did not answer (503)."""
