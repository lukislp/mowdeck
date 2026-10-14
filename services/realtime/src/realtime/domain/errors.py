"""Errors of the realtime use cases."""


class RealtimeError(Exception):
    """Base class; ``message`` is safe to show to the client."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidTokenError(RealtimeError):
    """The access token was not accepted (the reason is logged, never sent)."""


class LimitExceededError(RealtimeError):
    """A connection limit of the user or the account is reached."""


class DriveUnavailableError(RealtimeError):
    """Fleet could not be asked to open a drive session (no bus, timeout, no responder)."""


class FrameRejectedError(RealtimeError):
    """A frame that ends the connection: binary (4400) or too large (1009)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
