"""FastAPI dependencies: services, request context, the account in the path, error mapping."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from mowdeck_auth.step_up import require_recent_auth
from mowdeck_authz import ObjectRef, Relation, account
from mowdeck_authz.fastapi import require_or_not_found
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.correlation import get_correlation_id
from mowdeck_service_kit.errors import (
    Conflict,
    Forbidden,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    Unauthorized,
    ValidationFailed,
    problem_response,
)
from mowdeck_service_kit.tenancy import current_principal

from identity.application.context import RequestContext
from identity.application.services import Services
from identity.domain.errors import (
    AuthenticationFailed,
    AuthzUnavailable,
    ConflictError,
    ForbiddenError,
    IdentityError,
    InvalidInput,
    NotFoundError,
    RateLimited,
)

_log = get_logger("identity.api")


class TooManyRequests(ServiceError):  # noqa: N818 - HTTP status names read best
    """Too many attempts (429) with ``Retry-After``."""

    status = 429
    problem_type = "too-many-requests"
    title = "Too many requests"


def get_services(request: Request) -> Services:
    """The use cases created at startup (503 before startup finished)."""
    services: Services | None = getattr(request.app.state, "identity", None)
    if services is None:
        raise ServiceUnavailable("identity is starting")
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def client_ip(request: Request) -> str | None:
    """The client address; from ``X-Forwarded-For`` only when the proxy is trusted."""
    if getattr(request.app.state, "trust_forwarded_for", False):
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    return request.client.host if request.client else None


def _context(request: Request, principal: Principal | None) -> RequestContext:
    return RequestContext(
        principal=principal,
        ip=client_ip(request),
        correlation_id=get_correlation_id(),
        user_agent=request.headers.get("user-agent"),
    )


async def public_context(request: Request) -> RequestContext:
    """Context of a public endpoint (no principal)."""
    return _context(request, None)


async def principal_context(
    request: Request, principal: Annotated[Principal, Depends(current_principal)]
) -> RequestContext:
    """Context of a protected endpoint (401 without a valid token)."""
    return _context(request, principal)


PublicCtx = Annotated[RequestContext, Depends(public_context)]
Ctx = Annotated[RequestContext, Depends(principal_context)]

FreshAuth = Annotated[Principal, Depends(require_recent_auth())]
"""A critical action (ADR 0057): 401 ``reauth-required`` unless the caller confirmed with a
passkey or the password within the window, 403 for API tokens. Declare it **after** the
permission dependency so a caller without the right never learns about the re-authentication."""


async def path_account(request: Request) -> ObjectRef:
    """``account:<account_id>`` from the path; 404 unless it is the token's account.

    The account id still comes from the verified principal: a path naming another account is
    answered like a missing one, before OpenFGA is asked.
    """
    principal = await current_principal(request)
    raw = request.path_params.get("account_id")
    try:
        account_id = UUID(str(raw))
    except ValueError:
        raise NotFound("account not found") from None
    if principal.account_id != account_id:
        raise NotFound("account not found")
    return account(account_id)


def _on_account(relation: str) -> object:
    return Depends(require_or_not_found(relation, path_account, visible_relation=Relation.MEMBER))


Member = Annotated[Principal, _on_account(Relation.MEMBER)]
"""Any member of the account in the path (foreign accounts: 404)."""
ManageUsers = Annotated[Principal, _on_account(Relation.CAN_MANAGE_USERS)]
"""``can_manage_users`` on the account (members without it: 403, foreign: 404)."""
ViewAudit = Annotated[Principal, _on_account(Relation.CAN_VIEW_AUDIT)]
"""``can_view_audit`` on the account."""
ManageAccount = Annotated[Principal, _on_account(Relation.CAN_MANAGE_ACCOUNT)]
"""``can_manage_account`` (the owner)."""


def to_service_error(exc: IdentityError) -> ServiceError:
    """Map a use case error to the kit's problem types."""
    match exc:
        case AuthenticationFailed():
            return Unauthorized("invalid credentials")
        case RateLimited():
            return TooManyRequests(
                exc.message, headers={"Retry-After": str(exc.retry_after_seconds)}
            )
        case InvalidInput():
            return ValidationFailed(exc.message)
        case NotFoundError():
            return NotFound(exc.message)
        case ForbiddenError():
            return Forbidden(exc.message)
        case ConflictError():
            return Conflict(exc.message)
        case AuthzUnavailable():
            return ServiceUnavailable(exc.message)
        case _:
            return ServiceError(exc.message)


async def _identity_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, IdentityError)  # noqa: S101 - registered for IdentityError only
    if isinstance(exc, AuthenticationFailed):
        _log.info("authentication failed", reason=exc.reason, path=request.url.path)
    error = to_service_error(exc)
    return problem_response(error.to_problem(instance=request.url.path), error.headers)


def install_error_mapping(app: FastAPI) -> None:
    """Render :class:`IdentityError` as problem details."""
    app.add_exception_handler(IdentityError, _identity_error_handler)
