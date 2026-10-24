"""Public authentication endpoints, ``/me`` and the JWKS under ``/api/v1/identity``."""

from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Header, Request, Response
from mowdeck_authz import PLATFORM_OBJECT, CheckItem, ObjectRef, principal_subject
from mowdeck_service_kit.errors import BadRequest, ValidationFailed
from pydantic import BaseModel, Field

from identity.adapters.api.deps import Ctx, PublicCtx, ServicesDep
from identity.adapters.api.schemas import (
    AppCodeIn,
    AppCodeOut,
    AppSessionIn,
    LoginIn,
    MeOut,
    PasswordChangeIn,
    PermissionsIn,
    RefreshIn,
    RegisteredOut,
    RegisterIn,
    TokenExchangeIn,
    TokenOut,
    UiAreaOut,
)
from identity.application.accounts import Registration
from identity.application.sessions import TokenPair

router = APIRouter(prefix="/identity", tags=["identity: sessions"])

REFRESH_COOKIE = "mowdeck_refresh"
REFRESH_COOKIE_PATH = "/api/v1/identity/session"
JWKS_CACHE_CONTROL = "public, max-age=300"
UI_AREAS_CACHE_CONTROL = "private, max-age=300"
REFRESH_TRANSPORT_HEADER = "X-Mowdeck-Refresh"
COOKIE_ONLY = "cookie-only"
"""Value of :data:`REFRESH_TRANSPORT_HEADER` that keeps the refresh token out of the body."""

RefreshTransport = Annotated[
    str | None,
    Header(
        alias=REFRESH_TRANSPORT_HEADER,
        description="`cookie-only`: the refresh token is only set as the HttpOnly cookie and "
        "`refresh_token` in the body is null (browsers). Without it the body carries it too.",
    ),
]


def token_out(pair: TokenPair, transport: str | None) -> TokenOut:
    out = TokenOut.of(pair)
    if (transport or "").strip().lower() == COOKIE_ONLY:
        return out.model_copy(update={"refresh_token": None})
    return out


def set_refresh_cookie(request: Request, response: Response, pair: TokenPair) -> None:
    if pair.refresh_token is None or pair.refresh_expires_at is None:
        return
    max_age = int((pair.refresh_expires_at - pair.access.issued_at).total_seconds())
    response.set_cookie(
        REFRESH_COOKIE,
        pair.refresh_token,
        max_age=max(0, max_age),
        path=REFRESH_COOKIE_PATH,
        secure=getattr(request.app.state, "refresh_cookie_secure", True),
        httponly=True,
        samesite="strict",
    )


def _refresh_token(body: RefreshIn | None, cookie: str | None, requested_with: str | None) -> str:
    if body is not None and body.refresh_token:
        return body.refresh_token
    if cookie:
        if not requested_with:
            msg = "X-Requested-With is required when the refresh cookie is used"
            raise BadRequest(msg)
        return cookie
    msg = "refresh_token is required"
    raise ValidationFailed(msg)


@router.post("/register", status_code=201)
async def register(body: RegisterIn, services: ServicesDep, ctx: PublicCtx) -> RegisteredOut:
    """Create an account; the registering user becomes its owner."""
    registered = await services.accounts.register(
        Registration(
            username=body.username,
            password=body.password,
            account_name=body.account_name,
            display_name=body.display_name,
            email=body.email,
            kind=body.account_kind,
        ),
        ctx,
    )
    return RegisteredOut(account_id=registered.account_id, user_id=registered.user_id)


@router.post("/session")
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    services: ServicesDep,
    ctx: PublicCtx,
    transport: RefreshTransport = None,
) -> TokenOut:
    """Log in with username and password: access token, refresh token (cookie, and the body
    unless ``X-Mowdeck-Refresh: cookie-only``)."""
    pair = await services.sessions.login(body.username, body.password, body.account_id, ctx)
    set_refresh_cookie(request, response, pair)
    response.headers["Cache-Control"] = "no-store"
    return token_out(pair, transport)


@router.post("/session/refresh")
async def refresh(
    request: Request,
    response: Response,
    services: ServicesDep,
    ctx: PublicCtx,
    body: RefreshIn | None = None,
    cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    requested_with: Annotated[str | None, Header(alias="X-Requested-With")] = None,
    transport: RefreshTransport = None,
) -> TokenOut:
    """Rotate the refresh token; a reused token ends the whole login session (except within
    the short grace after a lost response)."""
    pair = await services.sessions.refresh(_refresh_token(body, cookie, requested_with), ctx)
    set_refresh_cookie(request, response, pair)
    response.headers["Cache-Control"] = "no-store"
    return token_out(pair, transport)


@router.post("/session/logout", status_code=204)
async def logout(
    response: Response,
    services: ServicesDep,
    ctx: PublicCtx,
    body: RefreshIn | None = None,
    cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    requested_with: Annotated[str | None, Header(alias="X-Requested-With")] = None,
) -> None:
    """End the login session of the refresh token."""
    await services.sessions.logout(_refresh_token(body, cookie, requested_with), ctx)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)


@router.post("/session/logout-all", status_code=204)
async def logout_all(services: ServicesDep, ctx: Ctx) -> None:
    """End every session of the calling user."""
    await services.sessions.logout_all(ctx)


@router.post("/app-login/code")
async def app_login_code(
    body: AppCodeIn, response: Response, services: ServicesDep, ctx: Ctx
) -> AppCodeOut:
    """Native app hand-off (ADR 0050), browser side: exchange the caller's fresh login session
    (any login method, at most a few minutes old) for a one-time code bound to the app's PKCE
    challenge. The browser session ends; the page sends the code to the app's URL scheme."""
    code = await services.sessions.issue_app_code(ctx, body.code_challenge)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    response.headers["Cache-Control"] = "no-store"
    ttl = int(services.deps.config.app_code_ttl.total_seconds())
    return AppCodeOut(code=code, expires_in=ttl)


@router.post("/app-login/session")
async def app_login_session(
    body: AppSessionIn,
    request: Request,
    response: Response,
    services: ServicesDep,
    ctx: PublicCtx,
    transport: RefreshTransport = None,
) -> TokenOut:
    """Native app hand-off, app side: the one-time code and the PKCE verifier give the app a
    normal session (the same tokens and cookie as ``POST /session``). The code is single use."""
    pair = await services.sessions.redeem_app_code(body.code, body.code_verifier, ctx)
    set_refresh_cookie(request, response, pair)
    response.headers["Cache-Control"] = "no-store"
    return token_out(pair, transport)


@router.post("/token")
async def exchange(
    body: TokenExchangeIn, response: Response, services: ServicesDep, ctx: PublicCtx
) -> TokenOut:
    """Exchange an API token for a 10 minute access token (``kind=api_token``)."""
    pair = await services.sessions.exchange(body.token, ctx)
    response.headers["Cache-Control"] = "no-store"
    return TokenOut.of(pair)


@router.get("/me")
async def me(services: ServicesDep, ctx: Ctx) -> MeOut:
    """The caller: claims of the current token, user data and memberships."""
    assert ctx.principal is not None  # noqa: S101 - Ctx requires a principal
    return MeOut.of(await services.sessions.me(ctx.principal))


class MePlatformOut(BaseModel):
    """The caller's role on the platform (operator console)."""

    operator_admin: bool = Field(
        description="True when the caller holds platform:main#operator_admin in OpenFGA."
    )


@router.get("/me/platform")
async def my_platform(services: ServicesDep, ctx: Ctx) -> MePlatformOut:
    """Whether the caller is an operator admin; 200 for every authenticated principal.

    Lets the web app decide about the operator console without probing a 403 endpoint. The
    check goes through the authorizer and its cache.
    """
    assert ctx.principal is not None  # noqa: S101 - Ctx requires a principal
    try:
        subject = principal_subject(ctx.principal)
    except ValueError:
        return MePlatformOut(operator_admin=False)
    allowed = await services.deps.authorizer.check(subject, "operator_admin", PLATFORM_OBJECT)
    return MePlatformOut(operator_admin=allowed)


@router.get("/ui-areas")
async def ui_areas(services: ServicesDep, ctx: Ctx, response: Response) -> list[UiAreaOut]:
    """Areas of the web app shell that the deployed modules announce and the caller may open.

    Merged from the module descriptors Identity polls (``MOWDECK_IDENTITY_MODULE_URLS``) and its
    own; an area is listed when its ``required_permission`` is among the caller's permissions.
    """
    assert ctx.principal is not None  # noqa: S101 - Ctx requires a principal
    held = set(ctx.principal.permissions)
    found: dict[str, UiAreaOut] = {}
    for descriptor in services.deps.catalogue.descriptors():
        for area in descriptor.ui_areas:
            if area.required_permission in held and area.route not in found:
                found[area.route] = UiAreaOut.of(descriptor.name, descriptor.version, area)
    response.headers["Cache-Control"] = UI_AREAS_CACHE_CONTROL
    return sorted(found.values(), key=lambda a: a.route)


@router.put("/me/password", status_code=204)
async def change_password(body: PasswordChangeIn, services: ServicesDep, ctx: Ctx) -> None:
    """Change the own password; the other sessions end."""
    await services.sessions.change_password(ctx, body.current_password, body.new_password)


@router.post("/me/permissions")
async def my_permissions(body: PermissionsIn, services: ServicesDep, ctx: Ctx) -> list[bool]:
    """Batch of "may I?" questions for rendering the UI (answers are not cached here)."""
    assert ctx.principal is not None  # noqa: S101 - Ctx requires a principal
    try:
        subject = principal_subject(ctx.principal)
    except ValueError:
        return [False] * len(body.checks)
    if not body.checks:
        return []
    items: list[Any] = [
        CheckItem(subject, q.relation, ObjectRef.parse(q.object)) for q in body.checks
    ]
    return await services.deps.authorizer.check_many(items)


async def jwks_document(services: ServicesDep, response: Response) -> dict[str, Any]:
    """The public signing keys (RFC 7517)."""
    response.headers["Cache-Control"] = JWKS_CACHE_CONTROL
    return services.deps.keys.jwks()


router.add_api_route(
    "/.well-known/jwks.json", jwks_document, methods=["GET"], tags=["identity: keys"]
)
