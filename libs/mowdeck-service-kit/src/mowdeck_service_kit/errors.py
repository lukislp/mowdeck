"""Uniform error handling: every error response is an RFC 9457 problem document.

Raise a :class:`ServiceError` subclass from application code; the handlers installed by
:func:`install_error_handlers` render it as ``application/problem+json``. Request validation
errors, Starlette HTTP exceptions (404 for unknown routes, 405, ...) and unexpected exceptions
are rendered the same way. Unexpected exceptions become a generic 500 without internals and are
logged with the correlation id.
"""

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mowdeck_contracts.v1 import (
    PROBLEM_JSON_MEDIA_TYPE,
    PROBLEM_TYPE_PREFIX,
    ProblemDetails,
    ProblemFieldError,
)
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mowdeck_service_kit.correlation import get_correlation_id
from mowdeck_service_kit.logging import get_logger

_log = get_logger(__name__)

_STATUS_TITLES: dict[int, str] = {
    400: "Bad request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    415: "Unsupported media type",
    422: "Validation failed",
    429: "Too many requests",
    500: "Internal server error",
    503: "Service unavailable",
}


class ServiceError(Exception):
    """Base class of errors that map to a problem response.

    Subclasses set ``status``, ``problem_type`` (appended to ``urn:mowdeck:problem:``) and
    ``title``. ``detail`` explains this occurrence and is shown to the client, so it must not
    contain secrets or internals.
    """

    status: ClassVar[int] = 500
    problem_type: ClassVar[str] = "internal"
    title: ClassVar[str] = "Internal server error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        errors: Sequence[ProblemFieldError] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.errors = list(errors) if errors else None
        self.headers = dict(headers or {})

    @property
    def type_uri(self) -> str:
        """The full problem type URI, e.g. ``urn:mowdeck:problem:not-found``."""
        return f"{PROBLEM_TYPE_PREFIX}{self.problem_type}"

    def to_problem(self, instance: str | None = None) -> ProblemDetails:
        """Render this error as :class:`ProblemDetails`."""
        return ProblemDetails(
            type=self.type_uri,
            title=self.title,
            status=self.status,
            detail=self.detail,
            instance=instance,
            correlation_id=get_correlation_id(),
            errors=self.errors,
        )


class BadRequest(ServiceError):  # noqa: N818 - HTTP status names read best
    """The request is malformed (400)."""

    status = 400
    problem_type = "bad-request"
    title = "Bad request"


class Unauthorized(ServiceError):  # noqa: N818 - HTTP status names read best
    """No valid credentials were presented (401)."""

    status = 401
    problem_type = "unauthorized"
    title = "Unauthorized"

    def __init__(
        self,
        detail: str | None = None,
        *,
        errors: Sequence[ProblemFieldError] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            detail, errors=errors, headers={"WWW-Authenticate": "Bearer", **(headers or {})}
        )


class Forbidden(ServiceError):  # noqa: N818 - HTTP status names read best
    """The principal is known but not allowed to do this (403)."""

    status = 403
    problem_type = "forbidden"
    title = "Forbidden"


class NotFound(ServiceError):  # noqa: N818 - HTTP status names read best
    """The resource does not exist or is not visible to the principal (404)."""

    status = 404
    problem_type = "not-found"
    title = "Not found"


class Conflict(ServiceError):  # noqa: N818 - HTTP status names read best
    """The request conflicts with the current state (409)."""

    status = 409
    problem_type = "conflict"
    title = "Conflict"


class ValidationFailed(ServiceError):  # noqa: N818 - HTTP status names read best
    """The input is well-formed but violates a rule (422)."""

    status = 422
    problem_type = "validation-failed"
    title = "Validation failed"


class ServiceUnavailable(ServiceError):  # noqa: N818 - HTTP status names read best
    """A dependency needed for this request is not available (503)."""

    status = 503
    problem_type = "unavailable"
    title = "Service unavailable"


def problem_response(
    problem: ProblemDetails, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """Build an ``application/problem+json`` response for ``problem``."""
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type=PROBLEM_JSON_MEDIA_TYPE,
        headers=dict(headers) if headers else None,
    )


def internal_error_problem(instance: str | None = None) -> ProblemDetails:
    """The generic 500 problem, deliberately without any detail about the cause."""
    return ProblemDetails(
        type=f"{PROBLEM_TYPE_PREFIX}internal",
        title=_STATUS_TITLES[500],
        status=500,
        detail="An unexpected error occurred. Quote the correlation id when reporting it.",
        instance=instance,
        correlation_id=get_correlation_id(),
    )


def _escape_pointer(token: object) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def field_errors_from_validation(errors: Sequence[Any]) -> list[ProblemFieldError]:
    """Convert FastAPI/Pydantic validation errors into problem field errors.

    Body locations become JSON Pointers (``#/items/0/name``); query, path, header and cookie
    locations become ``parameter`` names. Input values are never echoed back.
    """
    result: list[ProblemFieldError] = []
    for error in errors:
        loc = tuple(error.get("loc", ()))
        detail = str(error.get("msg", "invalid value"))
        code = error.get("type")
        if loc and loc[0] == "body":
            pointer = "#" + "".join(f"/{_escape_pointer(part)}" for part in loc[1:])
            result.append(ProblemFieldError(detail=detail, pointer=pointer, code=code))
        elif len(loc) > 1:
            parameter = ".".join(str(part) for part in loc[1:])
            result.append(ProblemFieldError(detail=detail, parameter=parameter, code=code))
        else:
            result.append(ProblemFieldError(detail=detail, code=code))
    return result


async def _service_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ServiceError)  # noqa: S101 - registered for ServiceError only
    if exc.status >= 500:
        _log.error("service error", status=exc.status, problem_type=exc.problem_type)
    return problem_response(exc.to_problem(instance=request.url.path), exc.headers)


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for it only
    problem = ProblemDetails(
        type=f"{PROBLEM_TYPE_PREFIX}validation-failed",
        title=_STATUS_TITLES[422],
        status=422,
        detail="The request is invalid.",
        instance=request.url.path,
        correlation_id=get_correlation_id(),
        errors=field_errors_from_validation(exc.errors()),
    )
    return problem_response(problem)


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - registered for it only
    status = exc.status_code
    title = _STATUS_TITLES.get(status, "Error")
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != title else None
    problem = ProblemDetails(
        type="about:blank",
        title=title,
        status=status,
        detail=detail,
        instance=request.url.path,
        correlation_id=get_correlation_id(),
    )
    return problem_response(problem, exc.headers)


def install_error_handlers(app: FastAPI) -> None:
    """Register the problem+json exception handlers on ``app``."""
    app.add_exception_handler(ServiceError, _service_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)


class UnhandledErrorMiddleware:
    """Pure ASGI middleware turning unexpected exceptions into a generic 500 problem.

    It must run inside :class:`~mowdeck_service_kit.correlation.CorrelationIdMiddleware` so
    the log line and the response carry the correlation id.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            _log.exception("unhandled exception", path=scope.get("path"))
            if response_started:
                raise
            response = problem_response(internal_error_problem(instance=scope.get("path")))
            await response(scope, receive, send)
