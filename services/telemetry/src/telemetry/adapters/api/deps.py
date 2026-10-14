"""FastAPI dependencies: the use cases, the object gate and error mapping.

Account scoping: the account is always the verified token's ``acc`` (``CurrentAccountId``);
nothing in a path or query can name another account. The mower in the path is guarded by
``require_or_not_found(can_view, mower)``: 404 for a mower the caller cannot see (other
accounts), so a foreign mower never reveals its existence. The queries still filter every row by
the account (third layer).
"""

from typing import Annotated, ClassVar
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from mowdeck_authz import ObjectRef, Relation, account, principal_subject
from mowdeck_authz.fastapi import get_authorizer, path_object, require_or_not_found
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit.errors import (
    NotFound,
    ServiceError,
    ServiceUnavailable,
    ValidationFailed,
    problem_response,
)

from telemetry.application.services import Services
from telemetry.domain.errors import (
    InvalidQueryError,
    NoSnapshotYetError,
    NoTelemetryYetError,
    TelemetryError,
)


class NoTelemetryProblem(NotFound):
    """The mower is visible but has not reported yet (404 with its own problem type)."""

    problem_type: ClassVar[str] = "no-telemetry-yet"
    title: ClassVar[str] = "No telemetry yet"


class NoMapProblem(NotFound):
    """The mower is visible but the gateway has not published its map yet."""

    problem_type: ClassVar[str] = "no-map-yet"
    title: ClassVar[str] = "No map yet"


class NoCapabilitiesProblem(NotFound):
    """The mower is visible but the gateway has not published its capabilities yet."""

    problem_type: ClassVar[str] = "no-capabilities-yet"
    title: ClassVar[str] = "No capabilities yet"


class NoPathProblem(NotFound):
    """The mower is visible but runs no job whose route the gateway knows."""

    problem_type: ClassVar[str] = "no-path"
    title: ClassVar[str] = "No mow path"


def get_services(request: Request) -> Services:
    """The use cases created at startup (503 before startup finished)."""
    services: Services | None = getattr(request.app.state, "telemetry", None)
    if services is None:
        raise ServiceUnavailable("telemetry is starting")
    return services


ServicesDep = Annotated[Services, Depends(get_services)]

MOWER = path_object("mower", "mower_id")

ViewMower = Annotated[Principal, Depends(require_or_not_found(Relation.CAN_VIEW, MOWER))]
"""``can_view`` on the mower in the path (404 otherwise)."""

ManageMower = Annotated[Principal, Depends(require_or_not_found(Relation.CAN_MANAGE_MOWERS, MOWER))]
"""``can_manage_mowers`` on the mower in the path (403 when visible without it, else 404)."""


async def visible_mower_ids(
    request: Request, principal: Principal, account_id: UUID
) -> set[UUID] | None:
    """Ids of the mowers the principal may view; None means "all mowers of the account".

    ``can_view`` on the account covers every mower below it, so the account filter of the
    query alone answers the list. Otherwise ``list_objects`` gives the visible ids, which the
    caller intersects with the account's rows.
    """
    authorizer = await get_authorizer(request)
    try:
        subject = principal_subject(principal)
    except ValueError:
        return set()
    if await authorizer.check(subject, Relation.CAN_VIEW, account(account_id)):
        return None
    found: set[UUID] = set()
    for value in await authorizer.list_objects(subject, Relation.CAN_VIEW, "mower"):
        try:
            found.add(UUID(ObjectRef.parse(value).id))
        except ValueError:
            continue
    return found


def to_service_error(exc: TelemetryError) -> ServiceError:
    """Map a use case error to the kit's problem types."""
    match exc:
        case NoTelemetryYetError():
            return NoTelemetryProblem(exc.message)
        case NoSnapshotYetError(kind="map"):
            return NoMapProblem(exc.message)
        case NoSnapshotYetError(kind="path"):
            return NoPathProblem(exc.message)
        case NoSnapshotYetError():
            return NoCapabilitiesProblem(exc.message)
        case InvalidQueryError():
            return ValidationFailed(exc.message)
        case _:
            return ServiceError(exc.message)


async def _error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, TelemetryError)  # noqa: S101 - registered for TelemetryError only
    error = to_service_error(exc)
    return problem_response(error.to_problem(instance=request.url.path), error.headers)


def install_error_mapping(app: FastAPI) -> None:
    """Render :class:`TelemetryError` as problem details."""
    app.add_exception_handler(TelemetryError, _error_handler)
