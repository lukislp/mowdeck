"""FastAPI dependencies: the use cases, the caller with its token, the error mapping."""

from typing import Annotated, ClassVar

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit import current_principal
from mowdeck_service_kit.errors import (
    Conflict,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    Unauthorized,
    ValidationFailed,
    problem_response,
)

from assistant.application.proposals import AssistantService
from assistant.domain.errors import (
    AssistantError,
    InvalidInput,
    NotFoundError,
    ProviderFailed,
    ProviderMissing,
    ProviderQuota,
    TooManyRequests,
    UpstreamUnavailable,
)


def get_service(request: Request) -> AssistantService:
    """The use cases (503 before startup finished)."""
    service: AssistantService | None = getattr(request.app.state, "assistant_service", None)
    if service is None:
        raise ServiceUnavailable("assistant is starting")
    return service


async def caller_token(request: Request) -> tuple[Principal, str]:
    """The verified caller and the bearer token it sent, forwarded unchanged to the public API
    so every read and every AI call runs with the caller's own rights."""
    principal = await current_principal(request)
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthorized("a bearer token is required")
    return principal, token.strip()


ServiceDep = Annotated[AssistantService, Depends(get_service)]
CallerDep = Annotated[tuple[Principal, str], Depends(caller_token)]


class ProviderMissingProblem(Conflict):
    """409: no AI provider may be used; the user has to configure one."""

    problem_type: ClassVar[str] = "assistant-provider-missing"
    title: ClassVar[str] = "No AI provider"


class TooManyProblem(ServiceError):  # noqa: N818 - a problem type, like the kit
    """429: the caller's or the provider's limit is reached."""

    status: ClassVar[int] = 429
    problem_type: ClassVar[str] = "assistant-limit"
    title: ClassVar[str] = "Limit reached"


class ProviderFailedProblem(ServiceError):  # noqa: N818 - a problem type, like the kit
    """502: the AI model gave no usable answer."""

    status: ClassVar[int] = 502
    problem_type: ClassVar[str] = "assistant-provider-failed"
    title: ClassVar[str] = "The AI model failed"


def to_service_error(exc: AssistantError) -> ServiceError:
    """Map a use case error to a problem type."""
    match exc:
        case NotFoundError():
            return NotFound(exc.message)
        case InvalidInput():
            return ValidationFailed(exc.message)
        case ProviderMissing():
            return ProviderMissingProblem(exc.message)
        case ProviderQuota() | TooManyRequests():
            return TooManyProblem(exc.message)
        case ProviderFailed():
            return ProviderFailedProblem(exc.message)
        case UpstreamUnavailable():
            return ServiceUnavailable(exc.message)
        case _:
            return ServiceError(exc.message)


async def _error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AssistantError)  # noqa: S101 - registered for it only
    error = to_service_error(exc)
    return problem_response(error.to_problem(instance=request.url.path), error.headers)


def install_error_mapping(app: FastAPI) -> None:
    """Render :class:`AssistantError` as problem details."""
    app.add_exception_handler(AssistantError, _error_handler)
