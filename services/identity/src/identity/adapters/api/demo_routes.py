"""Public demo (ADR 0065): the one-click sign-in, the seed call and the demo policy.

Both endpoints answer 404 outside demo mode (``MOWDECK_DEMO_MODE``). In demo mode Identity
refuses every mutating request that :data:`DEMO_POLICY` does not allow: no registration, no
password, passkey or provider login, no tokens, no changes to users, roles, the account or its
security. A visitor only reaches the prepared demo account through ``POST /demo/session``.
"""

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from mowdeck_service_kit.demo import DemoPolicy, require_demo_seed, rule
from pydantic import BaseModel, Field

from identity.adapters.api.auth_routes import RefreshTransport, set_refresh_cookie, token_out
from identity.adapters.api.deps import PublicCtx, ServicesDep
from identity.adapters.api.schemas import TokenOut
from identity.application.accounts import Registration
from identity.application.context import SYSTEM
from identity.domain.credentials import normalize_username

router = APIRouter(prefix="/identity/demo", tags=["identity: demo"])

_P = "/api/v1/identity"

DEMO_POLICY = DemoPolicy(
    allow_only=(
        rule(rf"{_P}/demo/session", "", "POST"),
        rule(rf"{_P}/demo/seed", "", "POST"),  # needs the seed secret itself
        rule(rf"{_P}/session/refresh", "", "POST"),
        rule(rf"{_P}/session/logout", "", "POST"),
        rule(rf"{_P}/me/permissions", "", "POST"),
        rule(rf"{_P}/me/onboarding", "", "PUT"),
    ),
    default_reason=(
        "The demo is a showcase: accounts, sign-in methods, users, roles, tokens and security "
        "settings cannot be changed here. Install MowDeck at home to try them."
    ),
)
"""Identity in demo mode: only the demo sign-in, keeping the session and the setup tour."""


class DemoSeedOut(BaseModel):
    """The demo account and user (created now or before)."""

    account_id: str = Field(description="The demo account.")
    user_id: str = Field(description="The demo user every visitor signs in as.")
    created: bool = Field(description="True when this call created them.")


@router.post("/session")
async def demo_session(
    request: Request,
    response: Response,
    services: ServicesDep,
    ctx: PublicCtx,
    transport: RefreshTransport = None,
) -> TokenOut:
    """ "Try the demo": a session of the prepared demo user without credentials (the same tokens
    and cookie as ``POST /session``). 404 outside demo mode and while the demo is not seeded;
    rate limited per address."""
    pair = await services.sessions.login_demo(ctx)
    set_refresh_cookie(request, response, pair)
    response.headers["Cache-Control"] = "no-store"
    return token_out(pair, transport)


@router.post("/seed", include_in_schema=False)
async def demo_seed(
    _: Annotated[None, Depends(require_demo_seed)], request: Request, services: ServicesDep
) -> DemoSeedOut:
    """The seed job: create the demo account and user unless they exist (idempotent). The
    password is random and never stored anywhere else; password login is closed anyway."""
    config = services.deps.config
    settings = request.app.state.settings
    username = config.demo_username
    assert username is not None  # noqa: S101 - require_demo_seed ran
    async with services.deps.store.transaction() as tx:
        found = await tx.users.get(username_norm=normalize_username(username))
        memberships = await tx.memberships.find(user_id=found.id) if found else []
    if found is not None and memberships:
        return DemoSeedOut(
            account_id=str(memberships[0].account_id), user_id=str(found.id), created=False
        )
    registered = await services.accounts.register(
        Registration(
            username=username,
            password=secrets.token_urlsafe(32),
            account_name=settings.identity_demo_account_name,
            display_name=settings.identity_demo_display_name,
        ),
        SYSTEM,
        ignore_mode=True,
    )
    return DemoSeedOut(
        account_id=str(registered.account_id), user_id=str(registered.user_id), created=True
    )
