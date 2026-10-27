"""FastAPI dependencies: the use cases, the caller gate and error mapping.

Everything belongs to the caller: the user is the verified token's ``sub``, the account its
``acc``; nothing in a path, query or body can name another user or account. Resources of
someone else answer 404, exactly like resources that do not exist.
"""

from typing import Annotated, ClassVar

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from mowdeck_authz import Relation, principal_subject
from mowdeck_authz.fastapi import get_authorizer
from mowdeck_contracts.v1 import Principal, PrincipalKind
from mowdeck_service_kit import CurrentPrincipal
from mowdeck_service_kit.errors import (
    Forbidden,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    ValidationFailed,
    problem_response,
)

from notifications.application.services import Services
from notifications.domain.errors import (
    GoneError,
    InvalidInputError,
    NotFoundError,
    NotificationsError,
    RateLimitedError,
)
from notifications.module import MANAGE_PERMISSION


class TooManyRequests(ServiceError):  # noqa: N818 - HTTP status names read best
    """Rate limited (429, with ``Retry-After``)."""

    status: ClassVar[int] = 429
    problem_type: ClassVar[str] = "too-many-requests"
    title: ClassVar[str] = "Too many requests"


class Gone(ServiceError):  # noqa: N818 - HTTP status names read best
    """The resource is gone for good (410)."""

    status: ClassVar[int] = 410
    problem_type: ClassVar[str] = "gone"
    title: ClassVar[str] = "Gone"


def get_services(request: Request) -> Services:
    """The use cases created at startup (503 before startup finished)."""
    services: Services | None = getattr(request.app.state, "notifications", None)
    if services is None:
        raise ServiceUnavailable("notifications is starting")
    return services


ServicesDep = Annotated[Services, Depends(get_services)]

_PERSONS = frozenset({PrincipalKind.USER, PrincipalKind.SUBUSER})


async def notification_user(request: Request, principal: CurrentPrincipal) -> Principal:
    """A person of an account who may view mowers.

    Admitted: ``notifications.manage`` in the token (implied by ``mower.view`` on the whole
    account), or ``can_view`` on at least one mower (a viewer of one group). API tokens and
    services have no notifications (403).
    """
    if principal.kind not in _PERSONS or principal.account_id is None:
        raise Forbidden("notifications belong to users of an account")
    if MANAGE_PERMISSION in principal.permissions:
        return principal
    authorizer = await get_authorizer(request)
    visible = await authorizer.list_objects(
        principal_subject(principal), Relation.CAN_VIEW, "mower"
    )
    if visible:
        return principal
    raise Forbidden(f"missing permission {MANAGE_PERMISSION}")


NotificationUser = Annotated[Principal, Depends(notification_user)]


def to_service_error(exc: NotificationsError) -> ServiceError:
    """Map a use case error to the kit's problem types."""
    match exc:
        case NotFoundError():
            return NotFound(exc.message)
        case GoneError():
            return Gone(exc.message)
        case InvalidInputError():
            return ValidationFailed(exc.message)
        case RateLimitedError():
            seconds = max(1, int(exc.retry_after.total_seconds()))
            return TooManyRequests(exc.message, headers={"Retry-After": str(seconds)})
        case _:
            return ServiceError(exc.message)


async def _error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, NotificationsError)  # noqa: S101 - registered for it only
    error = to_service_error(exc)
    return problem_response(error.to_problem(instance=request.url.path), error.headers)


def install_error_mapping(app: FastAPI) -> None:
    """Render :class:`NotificationsError` as problem details."""
    app.add_exception_handler(NotificationsError, _error_handler)
