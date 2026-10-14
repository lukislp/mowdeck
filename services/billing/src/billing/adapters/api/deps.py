"""FastAPI dependencies: the use cases, the owner gate, the request base URL, error mapping.

* Account endpoints act on the account of the verified token (``acc``), never on a path value.
* ``can_manage_billing`` on that account (the owner, or a member granted ``billing.manage``;
  admins are excluded by the model) is required; a member without it gets 403, a principal who
  is not even a member of the token's account 404.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from mowdeck_authz import Relation
from mowdeck_authz.fastapi import principal_account, require_or_not_found
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit.errors import (
    BadRequest,
    Conflict,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    ValidationFailed,
    problem_response,
)

from billing.application.billing import BillingService
from billing.domain.errors import (
    BillingError,
    ConflictError,
    InvalidInput,
    InvalidSignature,
    NotFoundError,
    ProviderError,
)


def get_service(request: Request) -> BillingService:
    """The use cases created at startup (503 before startup finished)."""
    service: BillingService | None = getattr(request.app.state, "billing_service", None)
    if service is None:
        raise ServiceUnavailable("billing is starting")
    return service


ServiceDep = Annotated[BillingService, Depends(get_service)]

BillingManager = Annotated[
    Principal,
    Depends(require_or_not_found(Relation.CAN_MANAGE_BILLING, principal_account)),
]
"""``can_manage_billing`` on the token's account (403 for members without it, 404 otherwise)."""


def request_base(request: Request) -> str:
    """The public origin of the request (behind Traefik: forwarded proto and host)."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host:
        return str(request.base_url).rstrip("/")
    return f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}"


RequestBase = Annotated[str, Depends(request_base)]


class BadSignature(BadRequest):
    """400: the webhook signature (or payload) does not verify."""

    problem_type = "invalid-signature"
    title = "Invalid webhook signature"


def to_service_error(exc: BillingError) -> ServiceError:
    """Map a use case error to the kit's problem types."""
    match exc:
        case NotFoundError():
            return NotFound(exc.message)
        case InvalidInput():
            return ValidationFailed(exc.message)
        case ConflictError():
            return Conflict(exc.message)
        case InvalidSignature():
            return BadSignature(exc.message)
        case ProviderError():
            return ServiceUnavailable(exc.message)
        case _:
            return ServiceError(exc.message)


async def _error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, BillingError)  # noqa: S101 - registered for it only
    error = to_service_error(exc)
    return problem_response(error.to_problem(instance=request.url.path), error.headers)


def install_error_mapping(app: FastAPI) -> None:
    """Render :class:`BillingError` as problem details."""
    app.add_exception_handler(BillingError, _error_handler)
