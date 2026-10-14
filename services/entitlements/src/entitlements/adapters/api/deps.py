"""FastAPI dependencies: the use cases, the caller's account, the operator gate, error mapping.

* ``/me`` answers for the account of the verified token (``acc``), never for a path value.
* Operator endpoints need ``can_manage_plans`` on ``platform:main`` in OpenFGA (operator
  admins, written by ``python -m identity.cli grant-platform-admin``); account owners get 403.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from mowdeck_authz import PLATFORM_OBJECT, ObjectRef, Relation
from mowdeck_authz.fastapi import require
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit.errors import (
    NotFound,
    ServiceError,
    ServiceUnavailable,
    ValidationFailed,
    problem_response,
)

from entitlements.application.services import Services
from entitlements.domain.errors import EntitlementsError, InvalidInput, NotFoundError


def get_services(request: Request) -> Services:
    """The use cases created at startup (503 before startup finished)."""
    services: Services | None = getattr(request.app.state, "entitlements_services", None)
    if services is None:
        raise ServiceUnavailable("entitlements is starting")
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def platform_object(request: Request) -> ObjectRef:
    """Resolver of the operator endpoints: the single ``platform:main`` object."""
    del request
    return PLATFORM_OBJECT


Operator = Annotated[Principal, Depends(require(Relation.CAN_MANAGE_PLANS, platform_object))]
"""``can_manage_plans`` on ``platform:main`` (403 otherwise, 401 without a token)."""


def actor_of(principal: Principal) -> str:
    """``user:<id>`` (or ``api_token:<id>``) for the history."""
    prefix = "api_token" if principal.kind.value == "api_token" else "user"
    return f"{prefix}:{principal.subject}"


def to_service_error(exc: EntitlementsError) -> ServiceError:
    """Map a use case error to the kit's problem types."""
    match exc:
        case NotFoundError():
            return NotFound(exc.message)
        case InvalidInput():
            return ValidationFailed(exc.message)
        case _:
            return ServiceError(exc.message)


async def _error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, EntitlementsError)  # noqa: S101 - registered for it only
    error = to_service_error(exc)
    return problem_response(error.to_problem(instance=request.url.path), error.headers)


def install_error_mapping(app: FastAPI) -> None:
    """Render :class:`EntitlementsError` as problem details."""
    app.add_exception_handler(EntitlementsError, _error_handler)
