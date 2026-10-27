"""Step-up dependency: fresh, stale, missing, clock skew, API tokens, configuration."""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI
from mowdeck_auth import (
    ReauthRequired,
    StepUpUnavailable,
    ensure_recent_auth,
    install_auth,
    require_recent_auth,
    step_up_max_age,
)
from mowdeck_auth.testing import TestKeys, bearer
from mowdeck_contracts.v1 import Principal, PrincipalKind
from mowdeck_service_kit.errors import install_error_handlers

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _principal(age: timedelta | None, kind: PrincipalKind = PrincipalKind.USER) -> Principal:
    return Principal(
        subject=uuid4(),
        account_id=uuid4(),
        kind=kind,
        auth_time=NOW - age if age is not None else None,
        auth_methods=["passkey"] if age is not None else [],
    )


def test_fresh_stale_missing_and_future() -> None:
    assert ensure_recent_auth(_principal(timedelta(minutes=4)), now=NOW)
    with pytest.raises(ReauthRequired) as stale:
        ensure_recent_auth(_principal(timedelta(minutes=6)), now=NOW)
    assert 'max_age="300"' in stale.value.headers["WWW-Authenticate"]
    with pytest.raises(ReauthRequired):
        ensure_recent_auth(_principal(None), now=NOW)
    with pytest.raises(ReauthRequired):
        ensure_recent_auth(_principal(-timedelta(minutes=10)), now=NOW)
    assert ensure_recent_auth(_principal(timedelta(minutes=9)), timedelta(minutes=10), now=NOW)


def test_api_tokens_and_services_are_refused() -> None:
    for kind in (PrincipalKind.API_TOKEN, PrincipalKind.SERVICE):
        with pytest.raises(StepUpUnavailable):
            ensure_recent_auth(_principal(timedelta(seconds=1), kind), now=NOW)


def test_window_is_configurable_and_clamped() -> None:
    assert step_up_max_age({}) == timedelta(minutes=5)
    assert step_up_max_age({"MOWDECK_AUTH_STEP_UP_MAX_AGE_SECONDS": "120"}) == timedelta(minutes=2)
    assert step_up_max_age({"MOWDECK_AUTH_STEP_UP_MAX_AGE_SECONDS": "1"}) == timedelta(seconds=30)
    assert step_up_max_age({"MOWDECK_AUTH_STEP_UP_MAX_AGE_SECONDS": "x"}) == timedelta(minutes=5)


async def test_dependency_over_http() -> None:
    keys = TestKeys()
    app = FastAPI()
    install_error_handlers(app)
    install_auth(app, keys.verifier())
    app.state.auth_clock = lambda: NOW

    @app.delete("/thing")
    async def thing(_: Annotated[Principal, Depends(require_recent_auth())]) -> dict[str, bool]:
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:

        async def call(p: Principal) -> httpx.Response:
            return await c.delete("/thing", headers=bearer(keys.mint(p)))

        # keys.mint signs with the real clock; auth_time is relative to the fake clock only.
        ok = await call(_principal(timedelta(minutes=1)))
        stale = await call(_principal(timedelta(minutes=9)))
        api = await call(_principal(None, PrincipalKind.API_TOKEN))
    assert ok.status_code == 200
    assert stale.status_code == 401
    assert stale.json()["type"] == "urn:mowdeck:problem:reauth-required"
    assert api.status_code == 403
    assert api.json()["type"] == "urn:mowdeck:problem:step-up-unavailable"
