"""The security center of the signed-in user (ADR 0065) under ``/api/v1/identity/me``.

Everything here is the caller's own: their login sessions in every account, their security
events, their new-login alert setting. There is no admin view of another user's sessions. API
tokens get 403 (they have no sessions). Ending another session, signing out everywhere else and
turning the alerts off need a recent step-up (ADR 0057).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from mowdeck_auth.step_up import ensure_recent_auth

from identity.adapters.api.deps import Ctx, FreshAuth, ServicesDep
from identity.adapters.api.schemas import (
    SecurityAlertsIn,
    SecurityAlertsOut,
    SecurityEventOut,
    SecurityOverviewOut,
    SessionOut,
    SessionsRevokedOut,
)
from identity.application.security import EVENT_DAYS_MAX, EVENT_LIMIT_MAX

router = APIRouter(prefix="/identity/me", tags=["identity: security"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/security")
async def security_overview(
    services: ServicesDep, ctx: Ctx, response: Response
) -> SecurityOverviewOut:
    """Password status, the new-login alert setting and how many passkeys, linked providers,
    personal access tokens, sessions and known devices the caller has."""
    _no_store(response)
    return SecurityOverviewOut.of(await services.security.overview(ctx))


@router.put("/security/alerts")
async def set_security_alerts(
    body: SecurityAlertsIn, request: Request, services: ServicesDep, ctx: Ctx
) -> SecurityAlertsOut:
    """Turn the notification about a login from a new device on or off. Turning it off needs a
    recent step-up (401 `reauth-required`): a stolen session must not silence it."""
    if not body.new_login_alerts:
        assert ctx.principal is not None  # noqa: S101 - Ctx requires a principal
        clock = getattr(request.app.state, "auth_clock", None)
        ensure_recent_auth(ctx.principal, now=clock() if clock else None)
    enabled = await services.security.set_alerts(ctx, body.new_login_alerts)
    return SecurityAlertsOut(new_login_alerts=enabled)


@router.get("/sessions")
async def my_sessions(services: ServicesDep, ctx: Ctx, response: Response) -> list[SessionOut]:
    """The caller's live login sessions (browsers and app installations, all accounts), the
    current one first (`current`), then by last use."""
    _no_store(response)
    return [SessionOut.of(v) for v in await services.security.sessions(ctx)]


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(session_id: UUID, _: FreshAuth, services: ServicesDep, ctx: Ctx) -> None:
    """End one of the caller's login sessions: its refresh token fails at once, its access
    tokens are refused by Identity at once and by the other services when they expire (at most
    10 minutes). Idempotent; 404 for any other session. Needs a recent step-up."""
    await services.security.revoke(ctx, session_id)


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(
    _: FreshAuth, services: ServicesDep, ctx: Ctx
) -> SessionsRevokedOut:
    """Sign out everywhere else: every login session of the caller except this one ends (as
    `DELETE /me/sessions/{id}` does for one). Needs a recent step-up."""
    return SessionsRevokedOut(revoked=await services.security.revoke_others(ctx))


@router.get("/security-events")
async def my_security_events(
    services: ServicesDep,
    ctx: Ctx,
    response: Response,
    days: Annotated[int, Query(ge=1, le=EVENT_DAYS_MAX)] = 30,
    limit: Annotated[int, Query(ge=1, le=EVENT_LIMIT_MAX)] = 100,
) -> list[SecurityEventOut]:
    """The caller's own security events of the last `days` (default 30), newest first: logins
    and failed logins, new devices, passkeys, linked providers, password changes, personal
    access tokens, ended sessions. Addresses are shortened to their network."""
    _no_store(response)
    return [SecurityEventOut.of(e) for e in await services.security.events(ctx, days, limit)]
