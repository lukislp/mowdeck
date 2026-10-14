"""FastAPI dependencies: the use cases, the plan feature gate and the error mapping.

Account scoping: the account is always the verified token's ``acc`` (``CurrentAccountId``);
nothing in a path or body can name another account. Mowers the caller cannot see answer 404.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from mowdeck_contracts.v1 import Principal, ProblemFieldError
from mowdeck_service_kit.entitlements import feature_gate
from mowdeck_service_kit.errors import (
    Conflict,
    Forbidden,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    ValidationFailed,
    problem_response,
)

from planner.application.services import Services
from planner.domain.errors import (
    ConflictError,
    ForbiddenError,
    InvalidInput,
    NotFoundError,
    PlannerError,
    Unavailable,
)
from planner.module import SPOT_MOWING_FEATURE


def get_services(request: Request) -> Services:
    """The use cases created at startup (503 before startup finished)."""
    services: Services | None = getattr(request.app.state, "planner", None)
    if services is None:
        raise ServiceUnavailable("planner is starting")
    return services


ServicesDep = Annotated[Services, Depends(get_services)]
SpotMowing = Annotated[Principal, Depends(feature_gate(SPOT_MOWING_FEATURE))]
"""The principal, with the plan feature ``spot_mowing`` on its account (403 without)."""


def to_service_error(exc: PlannerError) -> ServiceError:
    """Map a use case error to the kit's problem types."""
    match exc:
        case NotFoundError():
            return NotFound(exc.message)
        case ForbiddenError():
            return Forbidden(exc.message)
        case ConflictError():
            return Conflict(exc.message)
        case InvalidInput():
            errors = [ProblemFieldError(detail=e[:500]) for e in exc.errors] or None
            return ValidationFailed(exc.message, errors=errors)
        case Unavailable():
            return ServiceUnavailable(exc.message)
        case _:
            return ServiceError(exc.message)


async def _error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, PlannerError)  # noqa: S101 - registered for PlannerError only
    error = to_service_error(exc)
    return problem_response(error.to_problem(instance=request.url.path), error.headers)


def install_error_mapping(app: FastAPI) -> None:
    """Render :class:`PlannerError` as problem details."""
    app.add_exception_handler(PlannerError, _error_handler)
