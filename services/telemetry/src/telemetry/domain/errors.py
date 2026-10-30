"""Errors of the use cases (mapped to problem details by the API adapter)."""


class TelemetryError(Exception):
    """Base class of telemetry errors."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NoTelemetryYetError(TelemetryError):
    """The mower is known and visible, but no state was received yet."""


class InvalidQueryError(TelemetryError):
    """A time range, step or limit is not acceptable."""


class UnknownMowerError(TelemetryError):
    """A command result names a mower that is not in the directory (yet)."""


class AccountMismatchError(TelemetryError):
    """A message names another account than the one that owns the mower."""


class NoSnapshotYetError(TelemetryError):
    """The mower is visible, but the gateway has not published this snapshot kind yet."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class UnknownTrackedMowerError(TelemetryError):
    """The mower is not in the directory of the account (theft tracking)."""


class NoTrackingError(TelemetryError):
    """The mower has no (matching) theft tracking session."""


class TrackingStateError(TelemetryError):
    """The request does not fit the session's state (e.g. a link on a deleted session)."""


class LinkLimitError(TelemetryError):
    """A session already has the most active share links."""


class RateLimitedError(TelemetryError):
    """Too many requests to the public tracking page."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AerialDisabledError(TelemetryError):
    """No aerial photo source is configured."""


class TileOutsideMapError(TelemetryError):
    """The tile is not around the mower's map or not in the configured zoom range."""


class AerialRateLimitedError(TelemetryError):
    """The account asked the photo source for too many new tiles."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AerialUpstreamError(TelemetryError):
    """The photo source did not deliver an image."""


class InvalidZoneError(TelemetryError):
    """A drawn zone is not acceptable (ADR 0070); ``field`` names the offending input."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class NoZoneError(TelemetryError):
    """The mower has no zone with this id."""


class ZonesUnavailableError(TelemetryError):
    """Virtual zones are not offered for this mower (its vendor shares a map, or it reports no
    position), or it has the most zones already."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason
