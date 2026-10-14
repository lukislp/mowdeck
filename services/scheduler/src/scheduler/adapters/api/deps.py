"""FastAPI dependencies: the use cases and the error mapping.

Account scoping: the account is always the verified token's ``acc`` (``CurrentAccountId``);
nothing in a path, query or body can name another account. Schedules of other accounts, and
schedules whose target the caller cannot see, answer 404.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from mowdeck_service_kit.errors import (
    Conflict,
    Forbidden,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    ValidationFailed,
    problem_response,
)

from scheduler.application.services import Services
from scheduler.domain.errors import (
    ConflictError,
    ForbiddenError,
    InvalidInput,
    NotFoundError,
    SchedulerError,
    Unavailable,
)


def get_services(request: Request) -> Services:
    """The use cases created at startup (503 before startup finished)."""
    services: Services | None = getattr(request.app.state, "scheduler", None)
    if services is None:
        raise ServiceUnavailable("scheduler is starting")
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def to_service_error(exc: SchedulerError) -> ServiceError:
    """Map a use case error to the kit's problem types."""
    match exc:
        case NotFoundError():
            return NotFound(exc.message)
        case ForbiddenError():
            return Forbidden(exc.message)
        case ConflictError():
            return Conflict(exc.message)
        case InvalidInput():
            return ValidationFailed(exc.message)
        case Unavailable():
            return ServiceUnavailable(exc.message)
        case _:
            return ServiceError(exc.message)


async def _error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, SchedulerError)  # noqa: S101 - registered for SchedulerError only
    error = to_service_error(exc)
    return problem_response(error.to_problem(instance=request.url.path), error.headers)


def install_error_mapping(app: FastAPI) -> None:
    """Render :class:`SchedulerError` as problem details."""
    app.add_exception_handler(SchedulerError, _error_handler)
