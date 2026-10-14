"""Errors of the vision use cases (mapped to problem details in the API adapter)."""


class VisionError(Exception):
    """Base class; ``message`` is safe to show to the caller (never a key or an image)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(VisionError):
    """The thing asked for does not exist in the caller's account (404)."""


class InvalidInput(VisionError):  # noqa: N818 - reads like the HTTP status it maps to
    """The request cannot be served as asked (422)."""


class ConflictError(VisionError):
    """The request conflicts with the current state (409), e.g. a proposal already waits."""


class ProposalExpired(ConflictError):  # noqa: N818 - reads as a state
    """The proposal can no longer be approved (409, ``vision-proposal-expired``)."""


class ProviderMissing(ConflictError):  # noqa: N818 - reads as a state
    """Neither an own provider nor a hosted default may be used (409,
    ``vision-provider-missing``)."""


class QuotaExceeded(VisionError):  # noqa: N818 - reads as a state
    """A daily cap or the test rate limit is reached (429, ``vision-quota``)."""


class ProviderFailed(VisionError):  # noqa: N818 - reads as an outcome
    """The vision model did not answer usefully (502 for the provider test)."""


class ProviderTimeout(ProviderFailed):
    """The model call did not finish within the use case's total time budget (504,
    ``vision-provider-timeout``); the web maps it like a dropped connection."""


class SecretsUnavailable(VisionError):  # noqa: N818 - reads as a state
    """API keys cannot be stored: ``MOWDECK_VISION_SECRETS_KEY`` is not set (409)."""


class PhotoUnavailable(VisionError):  # noqa: N818 - reads as a state
    """An automatic analysis needs a photo from the mower and none could be taken (never
    reaches the API: automatic analyses have no caller)."""
