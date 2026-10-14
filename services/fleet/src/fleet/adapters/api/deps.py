"""FastAPI dependencies: the use cases, the actor, object gates and error mapping.

Account scoping: the account is always the verified token's ``acc`` (``CurrentAccountId``);
nothing in a path, query or body can name another account. Object gates come from
``mowdeck_authz.fastapi``: ``require_or_not_found`` answers 403 when the caller sees the object
but lacks the relation and 404 when it does not see it (other accounts), so a foreign object
never reveals its existence. The use cases still load every row with the account filter.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from mowdeck_authz import Relation
from mowdeck_authz.fastapi import path_object, principal_account, require, require_or_not_found
from mowdeck_contracts.v1 import Principal, ProblemFieldError
from mowdeck_service_kit import CurrentPrincipal
from mowdeck_service_kit.correlation import get_correlation_id
from mowdeck_service_kit.entitlements import FeatureNotInPlan, PlanLimitReached
from mowdeck_service_kit.errors import (
    Conflict,
    Forbidden,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    ValidationFailed,
    problem_response,
)

from fleet.application.context import Actor
from fleet.application.services import Services
from fleet.application.throttle import TooManyLoginAttempts as LoginThrottled
from fleet.domain.errors import (
    BridgeScanUnsupportedError,
    CameraUnsupportedError,
    ConflictError,
    FeatureNotInPlanError,
    FirmwareCheckUnsupportedError,
    FleetError,
    ForbiddenError,
    GatewayTimeout,
    InvalidInput,
    LinkedAccountInUseError,
    LinkedAccountLoginError,
    LinkNotApplicableError,
    MowerAlreadyRegisteredError,
    NotFoundError,
    PathPreviewRejectedError,
    PathPreviewUnsupportedError,
    PlanLimitError,
    Unavailable,
)


class CameraUnsupported(Conflict):
    """409 ``urn:mowdeck:problem:camera-unsupported``: the mower's connection has no camera."""

    problem_type = "camera-unsupported"
    title = "Camera not supported"


class PathPreviewUnsupported(Conflict):
    """409 ``urn:mowdeck:problem:path-preview-unsupported``: the mower's connection cannot plan
    a route without starting."""

    problem_type = "path-preview-unsupported"
    title = "Path preview not supported"


class PathPreviewRejected(Conflict):
    """409 ``urn:mowdeck:problem:path-preview-rejected``: the mower refused to plan (a job runs,
    unknown areas, a pattern it cannot run)."""

    problem_type = "path-preview-rejected"
    title = "Path preview refused"


class LinkNotApplicable(Conflict):
    """409 ``urn:mowdeck:problem:link-not-applicable``: the mower's connection kind carries no
    such additional link (the connection-kind registry decides, ADR 0038)."""

    problem_type = "link-not-applicable"
    title = "Link not applicable"


class BridgeScanUnsupported(Conflict):
    """409 ``urn:mowdeck:problem:bridge-scan-unsupported``: the Device-Gateway cannot scan
    through this kind of bridge."""

    problem_type = "bridge-scan-unsupported"
    title = "Bridge scan not supported"


class FirmwareCheckUnsupported(Conflict):
    """409 ``urn:mowdeck:problem:firmware-check-unsupported``: the mower's connection cannot ask
    the vendor for firmware."""

    problem_type = "firmware-check-unsupported"
    title = "Firmware check not supported"


class GatewayTimedOut(ServiceError):  # noqa: N818 - HTTP status names read best
    """504 ``urn:mowdeck:problem:gateway-timeout``: the mower did not answer in time."""

    status = 504
    problem_type = "gateway-timeout"
    title = "The mower did not answer in time"


class LinkedAccountInUse(Conflict):
    """409 ``urn:mowdeck:problem:linked-account-in-use``: mowers still connect through it;
    ``errors`` names each (``pointer`` ``#/mowers/<id>``, ``detail`` its name)."""

    problem_type = "linked-account-in-use"
    title = "Linked account in use"


class MowerAlreadyRegistered(Conflict):
    """409 ``urn:mowdeck:problem:mower-already-registered``: this physical mower is registered
    already (in this or another MowDeck account, which is not named)."""

    problem_type = "mower-already-registered"
    title = "Mower already registered"


class TooManyLoginAttempts(ServiceError):  # noqa: N818 - HTTP status names read best
    """429 ``urn:mowdeck:problem:too-many-requests`` with ``Retry-After``: too many failed
    vendor logins for this e-mail."""

    status = 429
    problem_type = "too-many-requests"
    title = "Too many requests"


class LinkedAccountLoginFailed(ValidationFailed):
    """422 ``urn:mowdeck:problem:linked-account-login-failed``: the vendor cloud refused the
    e-mail or password."""

    problem_type = "linked-account-login-failed"
    title = "Login refused by the vendor cloud"


def get_services(request: Request) -> Services:
    """The use cases created at startup (503 before startup finished)."""
    services: Services | None = getattr(request.app.state, "fleet", None)
    if services is None:
        raise ServiceUnavailable("fleet is starting")
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def actor_of(principal: Principal) -> Actor:
    """The actor of a request (principal and correlation id)."""
    return Actor(principal=principal, correlation_id=get_correlation_id())


async def current_actor(principal: CurrentPrincipal) -> Actor:
    """The actor of a protected request (401 without a valid token)."""
    return actor_of(principal)


ActorDep = Annotated[Actor, Depends(current_actor)]

MOWER = path_object("mower", "mower_id")
SITE = path_object("site", "site_id")
GROUP = path_object("group", "group_id")


def _hide(relation: str, resolver: object) -> object:
    return Depends(require_or_not_found(relation, resolver))  # type: ignore[arg-type]


ViewMower = Annotated[Principal, _hide(Relation.CAN_VIEW, MOWER)]
"""``can_view`` on the mower in the path (404 otherwise)."""
ControlMower = Annotated[Principal, _hide(Relation.CAN_CONTROL, MOWER)]
"""``can_control`` on the mower (403 when visible, else 404)."""
ManageMower = Annotated[Principal, _hide(Relation.CAN_MANAGE_MOWERS, MOWER)]
"""``can_manage_mowers`` on the mower (403 when visible, else 404)."""
ViewSite = Annotated[Principal, _hide(Relation.CAN_VIEW, SITE)]
ManageSite = Annotated[Principal, _hide(Relation.CAN_MANAGE_MOWERS, SITE)]
ViewGroup = Annotated[Principal, _hide(Relation.CAN_VIEW, GROUP)]
ManageGroup = Annotated[Principal, _hide(Relation.CAN_MANAGE_MOWERS, GROUP)]
ManageAccount = Annotated[
    Principal, Depends(require(Relation.CAN_MANAGE_MOWERS, principal_account))
]
"""``can_manage_mowers`` on the caller's own account (403 otherwise)."""


def to_service_error(exc: FleetError) -> ServiceError:
    """Map a use case error to the kit's problem types."""
    match exc:
        case NotFoundError():
            return NotFound(exc.message)
        case PlanLimitError():
            return PlanLimitReached(exc.reply, exc.key)
        case FeatureNotInPlanError():
            return FeatureNotInPlan(exc.reply, exc.feature)
        case ForbiddenError():
            return Forbidden(exc.message)
        case CameraUnsupportedError():
            return CameraUnsupported(exc.message)
        case PathPreviewUnsupportedError():
            return PathPreviewUnsupported(exc.message)
        case PathPreviewRejectedError():
            return PathPreviewRejected(exc.message)
        case LinkNotApplicableError():
            return LinkNotApplicable(exc.message)
        case BridgeScanUnsupportedError():
            return BridgeScanUnsupported(exc.message)
        case FirmwareCheckUnsupportedError():
            return FirmwareCheckUnsupported(exc.message)
        case GatewayTimeout():
            return GatewayTimedOut(exc.message)
        case LinkedAccountInUseError():
            return LinkedAccountInUse(
                exc.message,
                errors=[
                    ProblemFieldError(detail=name, pointer=f"#/mowers/{mower_id}", code="in_use")
                    for mower_id, name in exc.mowers
                ],
            )
        case MowerAlreadyRegisteredError():
            return MowerAlreadyRegistered(exc.message)
        case LinkedAccountLoginError():
            return LinkedAccountLoginFailed(
                exc.message,
                errors=[
                    ProblemFieldError(detail=f.message, pointer=f"#/{f.path}", code=f.code)
                    for f in exc.fields
                ]
                or None,
            )
        case ConflictError():
            return Conflict(exc.message)
        case InvalidInput():
            errors = [
                ProblemFieldError(detail=f.message, pointer=f"#/{f.path}", code=f.code)
                for f in exc.fields
            ]
            return ValidationFailed(exc.message, errors=errors or None)
        case Unavailable():
            return ServiceUnavailable(exc.message)
        case _:
            return ServiceError(exc.message)


async def _throttle_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, LoginThrottled)  # noqa: S101 - registered for it only
    error = TooManyLoginAttempts(str(exc), headers={"Retry-After": str(exc.retry_after_seconds)})
    return problem_response(error.to_problem(instance=request.url.path), error.headers)


async def _fleet_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, FleetError)  # noqa: S101 - registered for FleetError only
    error = to_service_error(exc)
    return problem_response(error.to_problem(instance=request.url.path), error.headers)


def install_error_mapping(app: FastAPI) -> None:
    """Render :class:`FleetError` as problem details."""
    app.add_exception_handler(FleetError, _fleet_error_handler)
    app.add_exception_handler(LoginThrottled, _throttle_handler)
