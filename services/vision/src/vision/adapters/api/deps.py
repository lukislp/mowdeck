"""FastAPI dependencies: the use cases, the mower and proposal gates, error mapping.

* Mower routes check ``can_drive`` (actions) or ``can_view`` (reads) on ``mower:<id>``; a mower
  the caller cannot see answers 404, one it sees but may not drive 403.
* Proposal routes first load the proposal of the caller's account (404 otherwise), then check
  the relation on its mower the same way.
"""

from typing import Annotated, ClassVar
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from mowdeck_authz import Relation, mower
from mowdeck_authz.fastapi import authorize, path_object, require_or_not_found
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit import current_principal
from mowdeck_service_kit.errors import (
    Conflict,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    ValidationFailed,
    problem_response,
)

from vision.application.assist import VisionService
from vision.application.auto import AutoSettings
from vision.application.calibration import CalibrationService
from vision.application.providers import ProviderService
from vision.domain.errors import (
    ConflictError,
    InvalidInput,
    NotFoundError,
    ProposalExpired,
    ProviderFailed,
    ProviderMissing,
    ProviderTimeout,
    QuotaExceeded,
    SecretsUnavailable,
    VisionError,
)
from vision.domain.models import Proposal


def get_service(request: Request) -> VisionService:
    """The AI assist use cases (503 before startup finished)."""
    service: VisionService | None = getattr(request.app.state, "vision_service", None)
    if service is None:
        raise ServiceUnavailable("vision is starting")
    return service


def get_providers(request: Request) -> ProviderService:
    """The provider settings use cases."""
    providers: ProviderService | None = getattr(request.app.state, "provider_service", None)
    if providers is None:
        raise ServiceUnavailable("vision is starting")
    return providers


def get_auto_settings(request: Request) -> AutoSettings:
    """The opt-in use cases of automatic analyses."""
    auto: AutoSettings | None = getattr(request.app.state, "auto_settings", None)
    if auto is None:
        raise ServiceUnavailable("vision is starting")
    return auto


def get_calibration(request: Request) -> CalibrationService:
    """The panorama calibration use case (corresponding points)."""
    calibration: CalibrationService | None = getattr(request.app.state, "calibration_service", None)
    if calibration is None:
        raise ServiceUnavailable("vision is starting")
    return calibration


CalibrationDep = Annotated[CalibrationService, Depends(get_calibration)]
AutoSettingsDep = Annotated[AutoSettings, Depends(get_auto_settings)]
ServiceDep = Annotated[VisionService, Depends(get_service)]
ProvidersDep = Annotated[ProviderService, Depends(get_providers)]
CallerDep = Annotated[Principal, Depends(current_principal)]

MowerDriver = Annotated[
    Principal, Depends(require_or_not_found(Relation.CAN_DRIVE, path_object("mower", "mower_id")))
]
"""``can_drive`` on the mower of the path (403 visible but not allowed, 404 invisible)."""
MowerViewer = Annotated[
    Principal, Depends(require_or_not_found(Relation.CAN_VIEW, path_object("mower", "mower_id")))
]
"""``can_view`` on the mower of the path."""


async def _proposal_for(
    request: Request, proposal_id: UUID, service: VisionService, relation: str
) -> tuple[Principal, Proposal]:
    principal = await current_principal(request)
    proposal = await service.find(principal, proposal_id)
    await authorize(request, relation, mower(proposal.mower_id))
    return principal, proposal


async def proposal_driver(
    request: Request, proposal_id: UUID, service: ServiceDep
) -> tuple[Principal, Proposal]:
    """The caller and the proposal, with ``can_drive`` on its mower."""
    return await _proposal_for(request, proposal_id, service, Relation.CAN_DRIVE)


async def proposal_viewer(
    request: Request, proposal_id: UUID, service: ServiceDep
) -> tuple[Principal, Proposal]:
    """The caller and the proposal, with ``can_view`` on its mower."""
    return await _proposal_for(request, proposal_id, service, Relation.CAN_VIEW)


ProposalDriver = Annotated[tuple[Principal, Proposal], Depends(proposal_driver)]
ProposalViewer = Annotated[tuple[Principal, Proposal], Depends(proposal_viewer)]


class ProviderMissingProblem(Conflict):
    """409: no provider may be used; the user has to configure one."""

    problem_type: ClassVar[str] = "vision-provider-missing"
    title: ClassVar[str] = "No vision provider"


class ProposalExpiredProblem(Conflict):
    """409: the proposal can no longer be approved."""

    problem_type: ClassVar[str] = "vision-proposal-expired"
    title: ClassVar[str] = "Proposal expired"


class SecretsUnavailableProblem(Conflict):
    """409: keys cannot be stored on this installation."""

    problem_type: ClassVar[str] = "vision-secrets-unavailable"
    title: ClassVar[str] = "API keys cannot be stored"


class QuotaProblem(ServiceError):  # noqa: N818 - a problem type, like the kit
    """429: a daily cap or the test rate limit is reached."""

    status: ClassVar[int] = 429
    problem_type: ClassVar[str] = "vision-quota"
    title: ClassVar[str] = "Limit reached"


class ProviderFailedProblem(ServiceError):  # noqa: N818 - a problem type, like the kit
    """502: the vision model did not answer usefully."""

    status: ClassVar[int] = 502
    problem_type: ClassVar[str] = "vision-provider-failed"
    title: ClassVar[str] = "The vision model failed"


class ProviderTimeoutProblem(ServiceError):  # noqa: N818 - a problem type, like the kit
    """504: the model call did not finish within the use case's time budget."""

    status: ClassVar[int] = 504
    problem_type: ClassVar[str] = "vision-provider-timeout"
    title: ClassVar[str] = "The vision model took too long"


def to_service_error(exc: VisionError) -> ServiceError:
    """Map a use case error to a problem type."""
    match exc:
        case NotFoundError():
            return NotFound(exc.message)
        case InvalidInput():
            return ValidationFailed(exc.message)
        case ProviderMissing():
            return ProviderMissingProblem(exc.message)
        case ProposalExpired():
            return ProposalExpiredProblem(exc.message)
        case SecretsUnavailable():
            return SecretsUnavailableProblem(exc.message)
        case ConflictError():
            return Conflict(exc.message)
        case QuotaExceeded():
            return QuotaProblem(exc.message)
        case ProviderTimeout():
            return ProviderTimeoutProblem(exc.message)
        case ProviderFailed():
            return ProviderFailedProblem(exc.message)
        case _:
            return ServiceError(exc.message)


async def _error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, VisionError)  # noqa: S101 - registered for it only
    error = to_service_error(exc)
    return problem_response(error.to_problem(instance=request.url.path), error.headers)


def install_error_mapping(app: FastAPI) -> None:
    """Render :class:`VisionError` as problem details."""
    app.add_exception_handler(VisionError, _error_handler)
