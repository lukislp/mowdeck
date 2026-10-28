"""Step-up authentication (ADR 0053): critical actions need a recent confirmation."""

import base64
import json
from datetime import timedelta
from typing import Any

from identity_support import API, PASSWORD, bearer, harness
from passkey_support import SoftAuthenticator
from test_identity_passkeys import ORIGIN, add_passkey

REAUTH = "urn:mowdeck:problem:reauth-required"


def _claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    claims: dict[str, Any] = json.loads(
        base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    )
    return claims


def _token_body(account_id: object) -> dict[str, Any]:
    level = {"type": "account", "id": str(account_id)}
    return {"name": "ha", "grants": [{"level": level, "permissions": ["mower.view"]}]}


async def test_login_issues_auth_time_and_refresh_keeps_it() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        first = _claims(lukas.token)
        refreshed = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        second = _claims(refreshed.json()["access_token"])
    assert first["amr"] == ["pwd"]
    assert second["auth_time"] == first["auth_time"]
    assert second["amr"] == ["pwd"]


async def test_critical_actions_need_a_recent_confirmation_and_a_password_step_up_gives_it() -> (
    None
):
    async with harness() as h:
        # Log in 6 minutes "ago" (a token issued in the future would not verify), then come back.
        h.clock.advance(-timedelta(minutes=6))
        lukas = await h.register("lukas")
        fresh = await h.client.post(
            lukas.url("/api-tokens"), headers=lukas.headers, json=_token_body(lukas.account_id)
        )
        h.clock.advance(timedelta(minutes=6))
        stale = await h.client.post(
            lukas.url("/api-tokens"), headers=lukas.headers, json=_token_body(lukas.account_id)
        )
        wrong = await h.client.post(
            f"{API}/me/step-up/password", headers=lukas.headers, json={"password": "nope nope nope"}
        )
        step = await h.client.post(
            f"{API}/me/step-up/password", headers=lukas.headers, json={"password": PASSWORD}
        )
        new_headers = bearer(step.json()["access_token"])
        again = await h.client.post(
            lukas.url("/api-tokens"), headers=new_headers, json=_token_body(lukas.account_id)
        )
        audit = await h.audit(lukas)
    assert fresh.status_code == 201
    assert stale.status_code == 401
    assert stale.json()["type"] == REAUTH
    assert "max_age" in stale.headers["www-authenticate"]
    assert wrong.status_code == 401
    assert wrong.json()["type"] != REAUTH
    assert step.status_code == 200 and step.json()["refresh_token"] is None
    assert again.status_code == 201, again.text
    actions = {e["action"] for e in audit}
    assert {"step_up.succeeded", "step_up.reauth_failed"} <= actions


async def test_each_critical_identity_endpoint_refuses_a_stale_session() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        anna_id, _ = await h.subuser(lukas, "anna")
        viewer = await h.role_id(lukas, "viewer")
        h.clock.advance(timedelta(minutes=6))
        level = {"type": "account", "id": str(lukas.account_id)}
        calls = [
            ("POST", lukas.url("/users"), {"username": "bob", "password": PASSWORD}),
            ("POST", lukas.url(f"/users/{anna_id}/password"), {"password": PASSWORD + "x"}),
            ("DELETE", lukas.url(f"/users/{anna_id}"), None),
            (
                "POST",
                lukas.url("/role-assignments"),
                {
                    "subject_type": "user",
                    "subject_id": str(anna_id),
                    "role_id": str(viewer),
                    "level": level,
                },
            ),
            ("DELETE", lukas.url(f"/role-assignments/{anna_id}"), None),
            ("PATCH", lukas.url(f"/roles/{viewer}"), {"description": "x"}),
            ("POST", lukas.url("/api-tokens"), _token_body(lukas.account_id)),
            ("DELETE", lukas.url(f"/users/{anna_id}/passkeys/{anna_id}"), None),
            ("DELETE", f"{API}/me/passkeys/{anna_id}", None),
            ("DELETE", f"{API}/me/external-identities/github", None),
            ("DELETE", lukas.url(""), None),
        ]
        results = []
        for method, url, body in calls:
            response = await h.client.request(method, url, headers=lukas.headers, json=body)
            results.append((method, url, response.status_code, response.json().get("type")))
    for method, url, status, kind in results:
        assert (status, kind) == (401, REAUTH), (method, url, status, kind)


async def test_passkey_step_up_only_with_the_callers_own_passkey() -> None:
    async with harness(identity_public_url=ORIGIN) as h:
        h.clock.advance(-timedelta(minutes=6))
        lukas = await h.register("lukas")
        mia = await h.register("mia")
        own, foreign = SoftAuthenticator(ORIGIN), SoftAuthenticator(ORIGIN)
        await add_passkey(h, lukas, own)
        await add_passkey(h, mia, foreign)
        h.clock.advance(timedelta(minutes=6))

        async def step(soft: SoftAuthenticator) -> Any:
            options = (
                await h.client.post(f"{API}/me/step-up/passkey/options", headers=lukas.headers)
            ).json()
            return await h.client.post(
                f"{API}/me/step-up/passkey",
                headers=lukas.headers,
                json={
                    "ceremony_id": options["ceremony_id"],
                    "credential": soft.get(options["public_key"]),
                },
            )

        refused = await step(foreign)
        done = await step(own)
        audit = await h.audit(lukas)
    assert refused.status_code == 401
    assert done.status_code == 200, done.text
    claims = _claims(done.json()["access_token"])
    assert claims["amr"] == ["passkey"]
    assert claims["auth_time"] > _claims(lukas.token)["auth_time"]
    actions = [e["action"] for e in audit]
    assert "step_up.succeeded" in actions and "step_up.failed" in actions


async def test_step_up_attempts_are_rate_limited() -> None:
    async with harness(identity_login_ip_burst=2, identity_login_ip_per_minute=1) as h:
        lukas = await h.register("lukas")
        answers = [
            await h.client.post(
                f"{API}/me/step-up/password",
                headers=lukas.headers,
                json={"password": "wrong wrong"},
            )
            for _ in range(4)
        ]
    assert [a.status_code for a in answers][-1] == 429


async def test_delete_account_with_password_or_with_a_recent_confirmation() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        h.clock.advance(timedelta(minutes=6))
        stale = await h.client.request("DELETE", lukas.url(""), headers=lukas.headers)
        with_password = await h.client.request(
            "DELETE", lukas.url(""), headers=lukas.headers, json={"password": PASSWORD}
        )
    assert stale.status_code == 401 and stale.json()["type"] == REAUTH
    assert with_password.status_code == 202


async def test_api_tokens_cannot_step_up_or_pass_the_check() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        created = await h.client.post(
            lukas.url("/api-tokens"), headers=lukas.headers, json=_token_body(lukas.account_id)
        )
        secret = created.json()["token"]
        exchanged = await h.client.post(
            f"{API}/token", json={"grant_type": "api_token", "token": secret}
        )
        api_headers = bearer(exchanged.json()["access_token"])
        step = await h.client.post(
            f"{API}/me/step-up/password", headers=api_headers, json={"password": PASSWORD}
        )
        delete = await h.client.request("DELETE", lukas.url(""), headers=api_headers)
    assert "auth_time" not in _claims(exchanged.json()["access_token"])
    assert step.status_code == 403
    assert delete.status_code in (403, 404)
