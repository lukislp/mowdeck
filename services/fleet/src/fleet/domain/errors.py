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


class Unavailable(FleetError):  # noqa: N818 - reads like the HTTP status it maps to
    """OpenFGA or the bus is not reachable; nothing was changed (503)."""


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


class MowerAlreadyRegisteredError(ConflictError):
    """The physical mower (vendor IoT id) is registered already, in this or another MowDeck
    account (409 ``mower-already-registered``); the message never names the other account."""
