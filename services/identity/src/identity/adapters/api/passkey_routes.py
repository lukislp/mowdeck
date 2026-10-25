"""Passkeys (ADR 0050) under ``/api/v1/identity``.

Login (public, rate limited per client IP like the password login; Traefik's ``identity-auth``
router covers ``/passkeys/``):

* ``POST /passkeys/challenge``: options for ``navigator.credentials.get()`` (no username).
* ``POST /passkeys/session``: the answer; starts the same session as ``POST /session``.

The native app runs this login in the system browser and takes it over with the generic
hand-off (``/app-login/*`` in ``auth_routes``).

Own passkeys (any signed-in user): ``/me/passkeys``. Adding one needs the current password.
Admins manage the passkeys of the account's sub-users under ``/accounts/{id}/users/{id}``
(see ``account_routes``).
"""

import json
from uuid import UUID

from fastapi import APIRouter, Request, Response

from identity.adapters.api.auth_routes import RefreshTransport, set_refresh_cookie, token_out
from identity.adapters.api.deps import Ctx, PublicCtx, ServicesDep
from identity.adapters.api.schemas import (
    LoginMethodsOut,
    PasskeyLoginIn,
    PasskeyOptionsOut,
    PasskeyOut,
    PasskeyPatch,
    PasskeyRegistrationIn,
    PasskeyRegistrationOptionsIn,
    TokenOut,
)

router = APIRouter(prefix="/identity", tags=["identity: passkeys"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/login-methods")
async def login_methods(services: ServicesDep) -> LoginMethodsOut:
    """Which login methods the installation offers (public; the login page asks)."""
    names = services.authenticators.names
    return LoginMethodsOut(
        password="password" in names,
        passkey="passkey" in names,
        github="github" in names,
        apple="apple" in names,
    )


@router.post("/passkeys/challenge")
async def passkey_challenge(
    services: ServicesDep, ctx: PublicCtx, response: Response
) -> PasskeyOptionsOut:
    """Start a passwordless login: options for ``navigator.credentials.get()`` with user
    verification required and no ``allowCredentials`` (discoverable credentials)."""
    challenge = await services.sessions.passkey_challenge(ctx)
    _no_store(response)
    return PasskeyOptionsOut(ceremony_id=challenge.ceremony_id, public_key=challenge.options)


@router.post("/passkeys/session")
async def passkey_login(
    body: PasskeyLoginIn,
    request: Request,
    response: Response,
    services: ServicesDep,
    ctx: PublicCtx,
    transport: RefreshTransport = None,
) -> TokenOut:
    """Finish a passwordless login: the same tokens and cookie as ``POST /session``."""
    pair = await services.sessions.login_passkey(
        body.ceremony_id, json.dumps(body.credential), body.account_id, ctx
    )
    set_refresh_cookie(request, response, pair)
    _no_store(response)
    return token_out(pair, transport)


# --- own passkeys -------------------------------------------------------------------------------


@router.get("/me/passkeys")
async def my_passkeys(services: ServicesDep, ctx: Ctx) -> list[PasskeyOut]:
    """The caller's passkeys, newest first."""
    return [PasskeyOut.of(p) for p in await services.passkeys.list_own(ctx)]


@router.post("/me/passkeys/options")
async def passkey_registration_options(
    body: PasskeyRegistrationOptionsIn, services: ServicesDep, ctx: Ctx, response: Response
) -> PasskeyOptionsOut:
    """Start adding a passkey: the current password again, then options for
    ``navigator.credentials.create()`` (discoverable, user verification required)."""
    start = await services.passkeys.start_registration(ctx, body.password)
    _no_store(response)
    return PasskeyOptionsOut(ceremony_id=start.ceremony_id, public_key=start.options)


@router.post("/me/passkeys", status_code=201)
async def add_passkey(body: PasskeyRegistrationIn, services: ServicesDep, ctx: Ctx) -> PasskeyOut:
    """Finish adding a passkey (audited as ``passkey.added``)."""
    passkey = await services.passkeys.finish_registration(
        ctx, body.ceremony_id, body.credential, body.name
    )
    return PasskeyOut.of(passkey)


@router.patch("/me/passkeys/{passkey_id}")
async def rename_passkey(
    passkey_id: UUID, body: PasskeyPatch, services: ServicesDep, ctx: Ctx
) -> PasskeyOut:
    """Rename one of the caller's passkeys."""
    return PasskeyOut.of(await services.passkeys.rename_own(ctx, passkey_id, body.name))


@router.delete("/me/passkeys/{passkey_id}", status_code=204)
async def remove_passkey(passkey_id: UUID, services: ServicesDep, ctx: Ctx) -> None:
    """Remove one of the caller's passkeys; the password always stays."""
    await services.passkeys.remove_own(ctx, passkey_id)
