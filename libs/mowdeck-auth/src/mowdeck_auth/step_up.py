"""Step-up authentication: critical actions need a recent interactive confirmation (ADR 0058).

::

    from mowdeck_auth import require_recent_auth

    FreshAuth = Annotated[Principal, Depends(require_recent_auth())]

    @router.delete("/mowers/{mower_id}", status_code=204)
    async def delete_mower(mower_id: UUID, _: CanDelete, fresh: FreshAuth) -> None: ...

The access token carries ``auth_time`` (when the user last authenticated: login, or a step-up
confirmation with a passkey or the password) and ``amr``. The dependency compares ``auth_time``
with the clock:

* fresh (younger than ``max_age``): the request goes on,
* missing or too old: **401** ``urn:mowdeck:problem:reauth-required`` with the RFC 9470 challenge
  ``WWW-Authenticate: Bearer error="insufficient_user_authentication", max_age="300"``. The web
  app shows its re-authentication sheet, calls Identity's ``/me/step-up/*`` for a new access
  token and repeats the request,
* API tokens and service principals can never confirm: **403**
  ``urn:mowdeck:problem:step-up-unavailable``.

Put the permission dependency first: a caller without the right gets 403/404 before any
re-authentication is asked for.

The window is 5 minutes (``MOWDECK_AUTH_STEP_UP_MAX_AGE_SECONDS``, 30 to 3600).
"""

import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

from fastapi import Request
from mowdeck_contracts.v1 import Principal, PrincipalKind
from mowdeck_service_kit.errors import ServiceError
from mowdeck_service_kit.tenancy import current_principal

__all__ = [
    "DEFAULT_MAX_AGE",
    "ReauthRequired",
    "StepUpUnavailable",
    "ensure_recent_auth",
    "require_recent_auth",
    "step_up_max_age",
]

DEFAULT_MAX_AGE = timedelta(minutes=5)
"""Default freshness window."""
_ENV = "MOWDECK_AUTH_STEP_UP_MAX_AGE_SECONDS"
_MIN_SECONDS, _MAX_SECONDS = 30, 3600
_INTERACTIVE = frozenset({PrincipalKind.USER, PrincipalKind.SUBUSER})


class ReauthRequired(ServiceError):  # noqa: N818 - named after its problem type
    """The caller must confirm with a passkey or the password first (401)."""

    status = 401
    problem_type = "reauth-required"
    title = "Re-authentication required"

    def __init__(self, max_age: timedelta) -> None:
        seconds = int(max_age.total_seconds())
        super().__init__(
            "this action needs a recent confirmation with a passkey or the password",
            headers={
                "WWW-Authenticate": (
                    'Bearer error="insufficient_user_authentication", '
                    f'error_description="a recent authentication is required", max_age="{seconds}"'
                )
            },
        )
        self.max_age = max_age


class StepUpUnavailable(ServiceError):  # noqa: N818 - named after its problem type
    """The caller cannot confirm interactively, so the action is refused (403)."""

    status = 403
    problem_type = "step-up-unavailable"
    title = "Interactive sign-in required"


def step_up_max_age(environ: Mapping[str, str] | None = None) -> timedelta:
    """The configured window (default 5 minutes); invalid values fall back to the default."""
    raw = (environ if environ is not None else os.environ).get(_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_AGE
    try:
        seconds = int(raw)
    except ValueError:
        return DEFAULT_MAX_AGE
    return timedelta(seconds=min(max(seconds, _MIN_SECONDS), _MAX_SECONDS))


def ensure_recent_auth(
    principal: Principal, max_age: timedelta | None = None, *, now: datetime | None = None
) -> Principal:
    """``principal`` if it authenticated within ``max_age``; else the matching problem."""
    window = max_age if max_age is not None else step_up_max_age()
    if principal.kind not in _INTERACTIVE:
        msg = "this action needs an interactive sign-in; API tokens and services cannot confirm it"
        raise StepUpUnavailable(msg)
    moment = now or datetime.now(UTC)
    auth_time = principal.auth_time
    # A future auth_time (more than a minute ahead of this clock) is not trusted either.
    if (
        auth_time is None
        or moment - auth_time > window
        or auth_time - moment > timedelta(minutes=1)
    ):
        raise ReauthRequired(window)
    return principal


def require_recent_auth(
    max_age: timedelta | None = None,
) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: the principal, if it authenticated within ``max_age``.

    ``max_age`` defaults to :func:`step_up_max_age` (read per request, so tests and operators
    can change it). ``app.state.auth_clock`` (a callable returning an aware datetime) replaces
    the system clock in tests.
    """

    async def dependency(request: Request) -> Principal:
        principal = await current_principal(request)
        clock: Callable[[], datetime] | None = getattr(request.app.state, "auth_clock", None)
        return ensure_recent_auth(principal, max_age, now=clock() if clock else None)

    dependency.__name__ = "require_recent_auth"
    return dependency
