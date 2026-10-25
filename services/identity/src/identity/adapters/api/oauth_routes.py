"""Login providers (ADR 0051) under ``/api/v1/identity``: "Sign in with GitHub".

Sign-in (public):

* ``POST /oauth/{provider}/start``: the provider's authorization URL; sets the HttpOnly cookie
  ``mowdeck_oauth`` (``SameSite=Lax``, path ``/api/v1/identity/oauth/``, 10 minutes) with the
  PKCE verifier that binds the round trip to this browser. Looser rate limit (it checks nothing).
* ``GET /oauth/{provider}/callback``: where the provider sends the browser back (the callback
  URL registered at the provider). Login limit per address. Answers with a redirect (303):

  - sign-in: ``/login?<query>&signed_in_with=github`` with the refresh cookie (the same session
    as after a password login), or ``/login?<query>&sign_in_error=<code>&provider=github``;
  - link: ``/settings?linked=github#profile`` or
    ``/settings?link_error=<code>&provider=github#profile``.

Own links (signed-in users): ``GET /me/external-identities``, ``POST
/me/external-identities/{provider}/link`` (the current password again, then the round trip) and
``DELETE /me/external-identities/{provider}``. Nothing here ever shows the provider's settings
(client id, callback URL); ``GET /login-methods`` only says whether GitHub is on.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Request, Response
from fastapi.responses import RedirectResponse

from identity.adapters.api.auth_routes import set_refresh_cookie
from identity.adapters.api.deps import Ctx, PublicCtx, ServicesDep
from identity.adapters.api.schemas import (
    ExternalIdentityOut,
    ExternalLinkIn,
    OAuthStartIn,
    OAuthStartOut,
)
from identity.application.authenticators import RedirectStart
from identity.application.external import RedirectOutcome
from identity.domain.models import RedirectPurpose

router = APIRouter(prefix="/identity", tags=["identity: login providers"])

OAUTH_COOKIE = "mowdeck_oauth"
OAUTH_COOKIE_PATH = "/api/v1/identity/oauth/"
CALLBACK_PARAMS = ("state", "code", "error")
"""The only callback parameters read; anything else the provider adds is ignored."""


def _bind_browser(request: Request, response: Response, start: RedirectStart) -> OAuthStartOut:
    """The verifier as the binding cookie; ``Lax`` so it comes along on the provider's
    top-level redirect back (a ``Strict`` cookie would not)."""
    response.set_cookie(
        OAUTH_COOKIE,
        start.browser_binding,
        max_age=start.expires_in,
        path=OAUTH_COOKIE_PATH,
        secure=getattr(request.app.state, "refresh_cookie_secure", True),
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return OAuthStartOut(authorize_url=start.url, expires_in=start.expires_in)


def _target(outcome: RedirectOutcome) -> str:
    """The web app page after a callback: always a fixed local path, never a target the
    request brought along (the query of /login was reduced to known keys at the start)."""
    provider = outcome.provider
    if outcome.purpose is RedirectPurpose.LINK:
        if outcome.failure:
            return f"/settings?link_error={outcome.failure}&provider={provider}#profile"
        return f"/settings?linked={provider}#profile"
    base = f"{outcome.return_query}&" if outcome.return_query else ""
    if outcome.failure:
        return f"/login?{base}sign_in_error={outcome.failure}&provider={provider}"
    return f"/login?{base}signed_in_with={provider}"


@router.post("/oauth/{provider}/start")
async def oauth_start(
    provider: str,
    request: Request,
    response: Response,
    services: ServicesDep,
    ctx: PublicCtx,
    body: OAuthStartIn | None = None,
) -> OAuthStartOut:
    """Start signing in with a login provider (404 when it is not enabled): its authorization
    URL, and a cookie that binds the round trip to this browser."""
    start = await services.sessions.start_redirect_login(
        provider, body.return_query if body else None, ctx
    )
    return _bind_browser(request, response, start)


@router.get("/oauth/{provider}/callback", response_class=RedirectResponse, status_code=303)
async def oauth_callback(
    provider: str,
    request: Request,
    services: ServicesDep,
    ctx: PublicCtx,
    binding: Annotated[str | None, Cookie(alias=OAUTH_COOKIE)] = None,
) -> RedirectResponse:
    """The provider sends the browser back here: finish the sign-in (refresh cookie as after a
    password login) or the link, then redirect to the web app (303)."""
    params = {k: request.query_params[k] for k in CALLBACK_PARAMS if k in request.query_params}
    outcome = await services.external.finish(provider, params, binding, ctx)
    response = RedirectResponse(_target(outcome), status_code=303)
    response.delete_cookie(OAUTH_COOKIE, path=OAUTH_COOKIE_PATH)
    if outcome.pair is not None:
        set_refresh_cookie(request, response, outcome.pair)
    response.headers["Cache-Control"] = "no-store"
    # The code and state of this URL never travel on as a Referer.
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/me/external-identities")
async def my_external_identities(services: ServicesDep, ctx: Ctx) -> list[ExternalIdentityOut]:
    """The caller's linked accounts of login providers."""
    return [ExternalIdentityOut.of(link) for link in await services.external.list_own(ctx)]


@router.post("/me/external-identities/{provider}/link")
async def link_external_identity(
    provider: str,
    body: ExternalLinkIn,
    request: Request,
    response: Response,
    services: ServicesDep,
    ctx: Ctx,
) -> OAuthStartOut:
    """Start linking an account of a login provider to the caller: the current password again
    (the login limits apply), then the provider's authorization URL and the binding cookie.
    409 when an account of this provider is already linked."""
    start = await services.external.start_link(ctx, provider, body.password)
    return _bind_browser(request, response, start)


@router.delete("/me/external-identities/{provider}", status_code=204)
async def unlink_external_identity(provider: str, services: ServicesDep, ctx: Ctx) -> None:
    """Remove the caller's link to a login provider (always allowed; the password stays)."""
    await services.external.unlink(ctx, provider)
