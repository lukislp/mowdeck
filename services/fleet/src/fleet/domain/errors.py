"""Errors of the use cases. The API maps them to problem responses (``adapters.api.deps``)."""

from collections.abc import Sequence
from dataclasses import dataclass

from mowdeck_contracts.v1 import EntitlementCheckReply


@dataclass(frozen=True, slots=True)
class FieldProblem:
    """One invalid field of the request body: a ``/`` separated path, a message, a code."""

    path: str
    message: str
    code: str | None = None


class FleetError(Exception):
    """Base class; ``message`` is safe to show to the caller."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(FleetError):
    """The object does not exist in the caller's account (404)."""


class ForbiddenError(FleetError):
    """The caller sees the object but may not do this (403)."""


class ConflictError(FleetError):
    """The change conflicts with the current state, e.g. a duplicate name (409)."""


class InvalidInput(FleetError):  # noqa: N818 - reads like the HTTP status it maps to
    """The request is well-formed but not acceptable (422); ``fields`` name the bad fields."""

    def __init__(self, message: str, *, fields: Sequence[FieldProblem] = ()) -> None:
        super().__init__(message)
        self.fields = tuple(fields)


class CameraUnsupportedError(ConflictError):
    """The mower's connection has no camera (409 ``camera-unsupported``)."""


class CameraRelayUnsupportedError(ConflictError):
    """The camera relay has no source for the mower's camera provider and the caller asked for
    the relay only (409 ``camera-relay-unsupported``, ADR 0045)."""


class CameraBudgetError(ConflictError):
    """A camera limit refused the stream: the video budget, the battery or the daily minutes
    (409 ``camera-budget``, ADR 0045)."""


class LinkNotApplicableError(ConflictError):
    """The mower's connection kind carries no such link (409 ``link-not-applicable``, decided
    by the connection-kind registry, ADR 0038)."""


class DriveCalibrationNotApplicableError(ConflictError):
    """The mower has nowhere to keep a drive calibration: no drive link and a connection whose
    options take no extra keys (409 ``drive-calibration-not-applicable``, ADR 0042)."""


class BridgeScanUnsupportedError(ConflictError):
    """The Device-Gateway cannot scan through this kind of bridge (409
    ``bridge-scan-unsupported``)."""


class FirmwareCheckUnsupportedError(ConflictError):
    """The mower's connection cannot ask the vendor for firmware (409
    ``firmware-check-unsupported``)."""


class Unavailable(FleetError):  # noqa: N818 - reads like the HTTP status it maps to
    """OpenFGA or the bus is not reachable; nothing was changed (503)."""


class PathPreviewUnsupportedError(ConflictError):
    """The mower's connection cannot plan a route without starting (409
    ``path-preview-unsupported``)."""


class PathPreviewRejectedError(ConflictError):
    """The mower refused to plan: a job runs, or the areas or pattern do not fit (409
    ``path-preview-rejected``)."""


class GatewayTimeout(FleetError):  # noqa: N818 - reads like the HTTP status it maps to
    """The mower or the Device-Gateway did not answer in time (504)."""


class PlanLimitError(ConflictError):
    """The plan of the account allows no more of ``key`` (409 ``plan-limit``, ADR 0028)."""

    def __init__(self, reply: EntitlementCheckReply, key: str) -> None:
        super().__init__(f"the plan of the account allows no more ({key})")
        self.reply = reply
        self.key = key


class FeatureNotInPlanError(ForbiddenError):
    """The plan of the account does not include ``feature`` (403 ``feature-not-in-plan``)."""

    def __init__(self, reply: EntitlementCheckReply, feature: str) -> None:
        super().__init__(f"the plan of the account does not include {feature}")
        self.reply = reply
        self.feature = feature


class LinkedAccountLoginError(InvalidInput):
    """The vendor cloud refused the e-mail or password (422 ``linked-account-login-failed``)."""


class LinkedAccountInUseError(ConflictError):
    """Mowers still connect through the linked account (409 ``linked-account-in-use``);
    ``mowers`` lists their ids and names."""

    def __init__(self, message: str, mowers: Sequence[tuple[str, str]]) -> None:
        super().__init__(message)
        self.mowers = tuple(mowers)


class BaseStationAlreadyRegisteredError(ConflictError):
    """The physical RTK base station (vendor IoT id) is registered already, in this or another
    MowDeck account (409 ``base-station-already-registered``, ADR 0047); the message never names
    the other account."""


class BaseStationNotApplicableError(ConflictError):
    """The base station cannot serve this mower: a cloud base station needs a cloud mower of
    the same linked account, the simulated one a simulated mower (409
    ``base-station-not-applicable``, ADR 0047)."""


class BaseStationNotLinkedError(ConflictError):
    """No mower is linked to the base station (or the mower to none), so nothing can relay a
    request to it (409 ``base-station-not-linked``, ADR 0047)."""


class MowerAlreadyRegisteredError(ConflictError):
    """The physical mower (vendor IoT id) is registered already, in this or another MowDeck
    account (409 ``mower-already-registered``); the message never names the other account."""
