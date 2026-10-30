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
from mowdeck_contracts.v1 import Principal, ProblemFieldError
from mowdeck_service_kit.errors import (
    Conflict,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    ValidationFailed,
    problem_response,
)

from telemetry.application.services import Services
from telemetry.application.widget import BusyError
from telemetry.domain.errors import (
    AerialDisabledError,
    AerialRateLimitedError,
    AerialUpstreamError,
    InvalidQueryError,
    InvalidZoneError,
    LinkLimitError,
    NoSnapshotYetError,
    NoTelemetryYetError,
    NoTrackingError,
    NoZoneError,
    RateLimitedError,
    TelemetryError,
    TileOutsideMapError,
    TrackingStateError,
    UnknownTrackedMowerError,
    ZonesUnavailableError,
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


class NoTrackingProblem(NotFound):
    """The mower has no (matching) theft tracking session or link."""

    problem_type: ClassVar[str] = "no-tracking"
    title: ClassVar[str] = "No tracking session"


class NoZoneProblem(NotFound):
    """The mower has no virtual zone with this id."""

    problem_type: ClassVar[str] = "no-zone"
    title: ClassVar[str] = "No such zone"


class ZonesUnavailableProblem(Conflict):
    """Virtual zones are not offered for the mower, or it has the most already (409; the
    error's ``code`` says which: ``vendor_map``, ``no_position``, ``limit``)."""

    problem_type: ClassVar[str] = "zones-unavailable"
    title: ClassVar[str] = "Zones not available"


class TooManyRequests(ServiceError):  # noqa: N818 - HTTP status names read best
    """The public tracking page was asked too often (429)."""

    status: ClassVar[int] = 429
    problem_type: ClassVar[str] = "too-many-requests"
    title: ClassVar[str] = "Too many requests"


class AerialDisabledProblem(NotFound):
    """No aerial photo source is configured."""

    problem_type: ClassVar[str] = "aerial-disabled"
    title: ClassVar[str] = "No aerial photo"


class TileOutsideMapProblem(NotFound):
    """The tile is not around the mower's map."""

    problem_type: ClassVar[str] = "tile-outside-map"
    title: ClassVar[str] = "Tile outside the map"


class AerialRateLimitedProblem(ServiceError):  # noqa: N818 - named like the other problems
    """Too many new aerial tiles."""

    status: ClassVar[int] = 429
    problem_type: ClassVar[str] = "rate-limited"
    title: ClassVar[str] = "Too many requests"


class AerialUpstreamProblem(ServiceError):  # noqa: N818 - named like the other problems
    """The photo source failed."""

    status: ClassVar[int] = 502
    problem_type: ClassVar[str] = "aerial-source-failed"
    title: ClassVar[str] = "Aerial photo source failed"


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


ControlMower = Annotated[Principal, Depends(require_or_not_found(Relation.CAN_CONTROL, MOWER))]
"""``can_control`` on the mower in the path (403 when visible without it, else 404)."""


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
        case InvalidZoneError():
            return ValidationFailed(
                exc.message,
                errors=[ProblemFieldError(detail=exc.message, pointer=f"#/{exc.field}")],
            )
        case NoZoneError():
            return NoZoneProblem(exc.message)
        case ZonesUnavailableError():
            return ZonesUnavailableProblem(
                exc.message, errors=[ProblemFieldError(detail=exc.message, code=exc.reason)]
            )
        case UnknownTrackedMowerError() | NoTrackingError():
            return NoTrackingProblem(exc.message)
        case TrackingStateError() | LinkLimitError():
            return Conflict(exc.message)
        case RateLimitedError():
            return TooManyRequests(
                exc.message,
                headers={
                    "Retry-After": str(exc.retry_after),
                    "Cache-Control": "no-store",
                    "X-Robots-Tag": "noindex, nofollow, noarchive",
                },
            )
        case BusyError():
            return ServiceUnavailable(exc.message)
        case AerialDisabledError():
            return AerialDisabledProblem(exc.message)
        case TileOutsideMapError():
            return TileOutsideMapProblem(exc.message)
        case AerialRateLimitedError():
            return AerialRateLimitedProblem(
                exc.message, headers={"Retry-After": str(exc.retry_after)}
            )
        case AerialUpstreamError():
            return AerialUpstreamProblem(exc.message)
        case _:
            return ServiceError(exc.message)


async def _error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, TelemetryError)  # noqa: S101 - registered for TelemetryError only
    error = to_service_error(exc)
    return problem_response(error.to_problem(instance=request.url.path), error.headers)


def install_error_mapping(app: FastAPI) -> None:
    """Render :class:`TelemetryError` as problem details."""
    app.add_exception_handler(TelemetryError, _error_handler)
