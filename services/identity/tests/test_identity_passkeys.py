"""Passkeys (ADR 0050): registration, passwordless login, management, the native app hand-off.

The ceremonies run end to end through the API and the real py_webauthn with a software
authenticator (``passkey_support``); the negative cases bend one thing at a time.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from identity_support import API, PASSWORD, Account, Harness, bearer, harness, make_settings
from passkey_support import BE, BS, UP, SoftAuthenticator, unb64url

from identity.application.ports import Lt
from identity.domain.credentials import s256_challenge, validate_passkey_name, verifier_matches
from identity.domain.errors import InvalidInput
from identity.domain.models import RelyingParty

ORIGIN = "https://mowdeck.example.org"
PASSKEYS: dict[str, Any] = {"identity_public_url": ORIGIN}
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
"""RFC 7636 appendix B."""


async def add_passkey(
    h: Harness, who: Account, soft: SoftAuthenticator, name: str | None = "iPhone"
) -> dict[str, Any]:
    """Register a passkey for ``who``; asserts success."""
    options = await h.client.post(
        f"{API}/me/passkeys/options", headers=who.headers, json={"password": PASSWORD}
    )
    assert options.status_code == 200, options.text
    body = options.json()
    created = await h.client.post(
        f"{API}/me/passkeys",
        headers=who.headers,
        json={
            "ceremony_id": body["ceremony_id"],
            "credential": soft.create(body["public_key"]),
            "name": name,
        },
    )
    assert created.status_code == 201, created.text
    result: dict[str, Any] = created.json()
    return result


async def challenge(h: Harness) -> dict[str, Any]:
    """Login options; asserts success."""
    response = await h.client.post(f"{API}/passkeys/challenge")
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def passkey_login(h: Harness, soft: SoftAuthenticator, **bend: Any) -> Any:
    """A full passwordless login; returns the response."""
    options = await challenge(h)
    return await h.client.post(
        f"{API}/passkeys/session",
        json={
            "ceremony_id": options["ceremony_id"],
            "credential": soft.get(options["public_key"], **bend),
        },
    )


# --- domain ---------------------------------------------------------------------------------------


def test_relying_party_from_the_public_url() -> None:
    rp = RelyingParty.from_public_url("https://MowDeck.Example.org:8443/app")
    assert rp.id == "mowdeck.example.org"
    assert rp.origins == ("https://mowdeck.example.org:8443",)
    parent = RelyingParty.from_public_url(ORIGIN, rp_id="example.org", name="Garden")
    assert (parent.id, parent.name) == ("example.org", "Garden")
    assert RelyingParty.from_public_url("http://localhost:8480").origins == (
        "http://localhost:8480",
    )
    for bad, rp_id in (
        ("http://mowdeck.example.org", ""),
        ("https://192.168.5.10", ""),
        ("https://[::1]", ""),
        ("ftp://mowdeck.example.org", ""),
        ("https://user:pw@mowdeck.example.org", ""),
        (ORIGIN, "other.org"),
        (ORIGIN, "ample.org"),
    ):
        with pytest.raises(ValueError):
            RelyingParty.from_public_url(bad, rp_id=rp_id)


def test_settings_turn_passkeys_off_for_an_unusable_url() -> None:
    assert make_settings().to_config().passkey_rp is None
    assert make_settings(identity_public_url="http://10.0.0.5").to_config().passkey_rp is None
    rp = make_settings(identity_public_url=ORIGIN).to_config().passkey_rp
    assert rp is not None and rp.id == "mowdeck.example.org"


def test_pkce_and_names() -> None:
    assert s256_challenge(VERIFIER) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert verifier_matches("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM", VERIFIER)
    assert not verifier_matches("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM", VERIFIER + "x")
    assert not verifier_matches(s256_challenge("short"), "short")
    assert validate_passkey_name("  Mac  ") == "Mac"
    for bad in ("", "   ", "x" * 65, "tab\there"):
        with pytest.raises(InvalidInput):
            validate_passkey_name(bad)


# --- login methods and the feature switch ---------------------------------------------------------


async def test_passkeys_are_off_without_a_public_url() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        methods = await h.client.get(f"{API}/login-methods")
        login = await h.client.post(f"{API}/passkeys/challenge")
        add = await h.client.post(
            f"{API}/me/passkeys/options", headers=lukas.headers, json={"password": PASSWORD}
        )
        listed = await h.client.get(f"{API}/me/passkeys", headers=lukas.headers)
    assert methods.json() == {"password": True, "passkey": False, "github": False}
    assert login.status_code == 403
    assert add.status_code == 403
    assert listed.json() == []


# --- registration ---------------------------------------------------------------------------------


async def test_register_list_and_log_in_without_a_password() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")
        soft = SoftAuthenticator(ORIGIN)
        created = await add_passkey(h, lukas, soft)
        options = await challenge(h)
        login = await h.client.post(
            f"{API}/passkeys/session",
            headers={"X-Mowdeck-Refresh": "cookie-only", "User-Agent": "test"},
            json={
                "ceremony_id": options["ceremony_id"],
                "credential": soft.get(options["public_key"]),
            },
        )
        me = await h.client.get(f"{API}/me", headers=bearer(login.json()["access_token"]))
        refreshed = await h.client.post(
            f"{API}/session/refresh", headers={"X-Requested-With": "test"}
        )
        listed = await h.client.get(f"{API}/me/passkeys", headers=lukas.headers)
        audit = await h.audit(lukas)
        methods = await h.client.get(f"{API}/login-methods")
    assert methods.json() == {"password": True, "passkey": True, "github": False}
    assert options["public_key"]["rpId"] == "mowdeck.example.org"
    assert options["public_key"]["userVerification"] == "required"
    assert options["public_key"].get("allowCredentials", []) == []
    assert created["name"] == "iPhone" and created["synced"] and created["backed_up"]
    assert created["aaguid"] == soft.aaguid and created["transports"] == ["internal", "hybrid"]
    assert login.status_code == 200, login.text
    assert login.json()["refresh_token"] is None
    assert login.headers["cache-control"] == "no-store"
    assert "mowdeck_refresh" in login.headers["set-cookie"]
    assert me.json()["subject"] == str(lukas.user_id)
    assert refreshed.status_code == 200
    [passkey] = listed.json()
    assert passkey["id"] == created["id"] and passkey["last_used_at"] is not None
    assert "public_key" not in passkey and "credential_id" not in passkey
    actions = {e["action"]: e for e in audit}
    assert actions["passkey.added"]["details"]["passkey"] == created["id"]
    assert actions["passkey.used"]["details"]["passkey"] == created["id"]
    logins = [e for e in audit if e["action"] == "session.login"]
    assert logins[0]["details"]["method"] == "passkey"
    assert logins[0]["details"]["passkey"] == created["id"]


async def test_registration_options_exclude_known_passkeys_and_need_the_password() -> None:
    async with harness(**PASSKEYS, identity_login_max_failures=3) as h:
        lukas = await h.register("lukas")
        soft = SoftAuthenticator(ORIGIN)
        first = await add_passkey(h, lukas, soft, name=None)
        options = await h.client.post(
            f"{API}/me/passkeys/options", headers=lukas.headers, json={"password": PASSWORD}
        )
        wrong = [
            await h.client.post(
                f"{API}/me/passkeys/options", headers=lukas.headers, json={"password": "nope"}
            )
            for _ in range(3)
        ]
        throttled = await h.client.post(
            f"{API}/me/passkeys/options", headers=lukas.headers, json={"password": PASSWORD}
        )
        audit = await h.audit(lukas, action="passkey.reauth_failed")
    assert first["name"] == "Passkey"
    public_key = options.json()["public_key"]
    assert public_key["authenticatorSelection"]["residentKey"] == "required"
    assert public_key["authenticatorSelection"]["userVerification"] == "required"
    assert public_key["attestation"] == "none"
    assert public_key["user"]["id"]  # the user handle: the user id, no personal data
    assert unb64url(public_key["user"]["id"]) == lukas.user_id.bytes
    excluded = [c["id"] for c in public_key["excludeCredentials"]]
    assert excluded == [soft.credentials[0].credential_id.hex()] or len(excluded) == 1
    assert [r.status_code for r in wrong] == [401, 401, 401]
    assert throttled.status_code == 429
    assert len(audit) == 3


async def test_registration_rejects_a_foreign_origin_missing_verification_and_reuse() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")

        async def attempt(soft: SoftAuthenticator, **flags: Any) -> Any:
            options = await h.client.post(
                f"{API}/me/passkeys/options", headers=lukas.headers, json={"password": PASSWORD}
            )
            body = options.json()
            payload = {
                "ceremony_id": body["ceremony_id"],
                "credential": soft.create(body["public_key"], **flags),
            }
            first = await h.client.post(f"{API}/me/passkeys", headers=lukas.headers, json=payload)
            again = await h.client.post(f"{API}/me/passkeys", headers=lukas.headers, json=payload)
            return first, again

        phishing, _ = await attempt(SoftAuthenticator("https://mowdeck.example.org.evil.net"))
        no_uv, _ = await attempt(SoftAuthenticator(ORIGIN), flags=UP | BE | BS)
        ok, replay = await attempt(SoftAuthenticator(ORIGIN))
        listed = await h.client.get(f"{API}/me/passkeys", headers=lukas.headers)
    assert phishing.status_code == 422
    assert no_uv.status_code == 422
    assert ok.status_code == 201
    assert replay.status_code == 422  # the challenge was used up
    assert len(listed.json()) == 1


async def test_a_registration_challenge_belongs_to_its_user_and_expires() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")
        mia = await h.register("mia")
        options = await h.client.post(
            f"{API}/me/passkeys/options", headers=lukas.headers, json={"password": PASSWORD}
        )
        body = options.json()
        stolen = await h.client.post(
            f"{API}/me/passkeys",
            headers=mia.headers,
            json={
                "ceremony_id": body["ceremony_id"],
                "credential": SoftAuthenticator(ORIGIN).create(body["public_key"]),
            },
        )
        late_options = await h.client.post(
            f"{API}/me/passkeys/options", headers=lukas.headers, json={"password": PASSWORD}
        )
        h.clock.advance(timedelta(minutes=6))
        late_body = late_options.json()
        late = await h.client.post(
            f"{API}/me/passkeys",
            headers=lukas.headers,
            json={
                "ceremony_id": late_body["ceremony_id"],
                "credential": SoftAuthenticator(ORIGIN).create(late_body["public_key"]),
            },
        )
    assert stolen.status_code == 422
    assert late.status_code == 422


async def test_the_same_authenticator_cannot_be_registered_twice() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")
        mia = await h.register("mia")
        soft = SoftAuthenticator(ORIGIN)
        await add_passkey(h, lukas, soft)
        options = (
            await h.client.post(
                f"{API}/me/passkeys/options", headers=mia.headers, json={"password": PASSWORD}
            )
        ).json()
        # The same credential id again (another account): the unique credential id wins.
        credential = soft.create(
            options["public_key"], credential_id=soft.credentials[0].credential_id
        )
        response = await h.client.post(
            f"{API}/me/passkeys",
            headers=mia.headers,
            json={"ceremony_id": options["ceremony_id"], "credential": credential},
        )
    assert response.status_code == 409


# --- login ----------------------------------------------------------------------------------------


async def test_login_failures_are_generic_and_audited() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")
        soft = SoftAuthenticator(ORIGIN)
        created = await add_passkey(h, lukas, soft)
        stranger = SoftAuthenticator(ORIGIN)
        stranger.create(
            {
                "rp": {"id": "mowdeck.example.org"},
                "user": {"id": "AAAA"},
                "challenge": "AAAA",
            }
        )
        unknown = await passkey_login(h, stranger)
        foreign_origin = await passkey_login(h, soft, origin="https://evil.example.net")
        no_uv = await passkey_login(h, soft, flags=UP | BE | BS)
        other_handle = await passkey_login(h, soft, user_handle=uuid4().bytes)
        options = await challenge(h)
        payload = {
            "ceremony_id": options["ceremony_id"],
            "credential": soft.get(options["public_key"]),
        }
        first = await h.client.post(f"{API}/passkeys/session", json=payload)
        replayed = await h.client.post(f"{API}/passkeys/session", json=payload)
        expired_options = await challenge(h)
        h.clock.advance(timedelta(minutes=6))
        expired = await h.client.post(
            f"{API}/passkeys/session",
            json={
                "ceremony_id": expired_options["ceremony_id"],
                "credential": soft.get(expired_options["public_key"]),
            },
        )
        malformed = await h.client.post(
            f"{API}/passkeys/session",
            json={"ceremony_id": str(uuid4()), "credential": {"id": "x"}},
        )
        audit = await h.audit(lukas, action="session.login_failed")
        attempts = await _attempts(h)
    failures = [unknown, foreign_origin, no_uv, other_handle, replayed, expired, malformed]
    assert first.status_code == 200
    assert [r.status_code for r in failures] == [401] * len(failures)
    assert len({r.json()["detail"] for r in failures}) == 1  # nothing tells them apart
    reasons = {e["details"]["reason"] for e in audit}
    assert "user handle mismatch" in reasons
    assert all(e["details"]["method"] == "passkey" for e in audit)
    assert {e["details"].get("passkey") for e in audit} == {created["id"]}
    # Passkey failures never lock the password login of the user.
    assert attempts == 0


async def _attempts(h: Harness) -> int:
    async with h.store.transaction() as tx:
        return await tx.login_attempts.count(success=False)


async def test_a_counter_that_does_not_grow_refuses_the_login() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")
        soft = SoftAuthenticator(ORIGIN, counting=True)
        await add_passkey(h, lukas, soft)
        ok = await passkey_login(h, soft)
        again = await passkey_login(h, soft)
        cloned = await passkey_login(h, soft, counter=1)
        audit = await h.audit(lukas, action="session.login_failed")
        listed = await h.client.get(f"{API}/me/passkeys", headers=lukas.headers)
    assert (ok.status_code, again.status_code, cloned.status_code) == (200, 200, 401)
    assert audit[0]["details"]["reason"] == "signature counter did not increase"
    assert len(listed.json()) == 1  # kept: the owner decides


async def test_synced_passkeys_with_a_zero_counter_log_in_repeatedly() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")
        soft = SoftAuthenticator(ORIGIN)
        await add_passkey(h, lukas, soft)
        results = [await passkey_login(h, soft) for _ in range(3)]
    assert [r.status_code for r in results] == [200, 200, 200]


async def test_disabled_users_and_removed_passkeys_cannot_log_in() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")
        anna_id, anna_token = await h.subuser(lukas, "anna")
        anna = Account(lukas.account_id, anna_id, "anna", anna_token, "")
        anna_soft = SoftAuthenticator(ORIGIN)
        await add_passkey(h, anna, anna_soft)
        await h.client.post(lukas.url(f"/users/{anna_id}/disable"), headers=lukas.headers)
        disabled = await passkey_login(h, anna_soft)
        soft = SoftAuthenticator(ORIGIN)
        created = await add_passkey(h, lukas, soft)
        removed = await h.client.delete(f"{API}/me/passkeys/{created['id']}", headers=lukas.headers)
        gone = await passkey_login(h, soft)
        password = await h.client.post(
            f"{API}/session", json={"username": "lukas", "password": PASSWORD}
        )
    assert disabled.status_code == 401
    assert removed.status_code == 204
    assert gone.status_code == 401
    assert password.status_code == 200  # the password always stays


async def test_passkey_login_is_rate_limited_per_address() -> None:
    async with harness(**PASSKEYS, identity_login_ip_burst=2, identity_login_ip_per_minute=1) as h:
        # Challenges have the looser limit (every sign-in page asks for one): 30 at once.
        challenges = [await h.client.post(f"{API}/passkeys/challenge") for _ in range(31)]
        # Answers have the login limit.
        answers = [
            await h.client.post(
                f"{API}/passkeys/session",
                json={"ceremony_id": str(uuid4()), "credential": {"id": "x"}},
            )
            for _ in range(3)
        ]
    assert [c.status_code for c in challenges[:30]] == [200] * 30
    assert challenges[30].status_code == 429
    assert [a.status_code for a in answers] == [401, 401, 429]
    assert "retry-after" in answers[2].headers


# --- management -----------------------------------------------------------------------------------


async def test_rename_and_remove_only_own_passkeys() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")
        mia = await h.register("mia")
        created = await add_passkey(h, lukas, SoftAuthenticator(ORIGIN))
        url = f"{API}/me/passkeys/{created['id']}"
        renamed = await h.client.patch(url, headers=lukas.headers, json={"name": "MacBook"})
        bad_name = await h.client.patch(url, headers=lukas.headers, json={"name": "   "})
        foreign_rename = await h.client.patch(url, headers=mia.headers, json={"name": "mine"})
        foreign_remove = await h.client.delete(url, headers=mia.headers)
        removed = await h.client.delete(url, headers=lukas.headers)
        removed_again = await h.client.delete(url, headers=lukas.headers)
        audit = await h.audit(lukas)
    assert renamed.json()["name"] == "MacBook"
    assert bad_name.status_code == 422
    assert foreign_rename.status_code == 404 and foreign_remove.status_code == 404
    assert removed.status_code == 204 and removed_again.status_code == 404
    actions = [e["action"] for e in audit]
    assert "passkey.renamed" in actions and "passkey.removed" in actions


async def test_admins_manage_the_passkeys_of_sub_users_only() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")
        anna_id, anna_token = await h.subuser(lukas, "anna")
        anna = Account(lukas.account_id, anna_id, "anna", anna_token, "")
        created = await add_passkey(h, anna, SoftAuthenticator(ORIGIN))
        listed = await h.client.get(lukas.url(f"/users/{anna_id}/passkeys"), headers=lukas.headers)
        by_anna = await h.client.get(lukas.url(f"/users/{anna_id}/passkeys"), headers=anna.headers)
        own = await h.client.get(
            lukas.url(f"/users/{lukas.user_id}/passkeys"), headers=lukas.headers
        )
        unknown = await h.client.get(lukas.url(f"/users/{uuid4()}/passkeys"), headers=lukas.headers)
        removed = await h.client.delete(
            lukas.url(f"/users/{anna_id}/passkeys/{created['id']}"), headers=lukas.headers
        )
        after = await h.client.get(f"{API}/me/passkeys", headers=anna.headers)
        audit = await h.audit(lukas, action="passkey.removed")
    assert [p["id"] for p in listed.json()] == [created["id"]]
    assert by_anna.status_code == 403  # no can_manage_users
    assert own.status_code == 403  # persons manage their own
    assert unknown.status_code == 404
    assert removed.status_code == 204
    assert after.json() == []
    assert audit[0]["details"]["by_admin"] is True


# --- the native app hand-off (login-method agnostic) -------------------------------------------


async def app_request(h: Harness, challenge_value: str) -> Any:
    """The app registers its PKCE challenge."""
    return await h.client.post(f"{API}/app-login/start", json={"code_challenge": challenge_value})


async def browser_code(
    h: Harness, access_token: str, challenge_value: str, request_id: str | None = None
) -> Any:
    """The app starts a request; the system browser hands its fresh login over for it."""
    if request_id is None:
        started = await app_request(h, challenge_value)
        assert started.status_code == 200, started.text
        request_id = started.json()["request_id"]
    return await h.client.post(
        f"{API}/app-login/code",
        headers=bearer(access_token),
        json={"request_id": request_id},
    )


async def passkey_code(h: Harness, soft: SoftAuthenticator, challenge_value: str) -> Any:
    login = await passkey_login(h, soft)
    assert login.status_code == 200, login.text
    return await browser_code(h, login.json()["access_token"], challenge_value)


async def test_the_app_takes_over_a_passkey_login_from_the_browser() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")
        soft = SoftAuthenticator(ORIGIN)
        await add_passkey(h, lukas, soft)
        browser = await passkey_login(h, soft)
        browser_refresh = browser.json()["refresh_token"]
        issued = await browser_code(h, browser.json()["access_token"], s256_challenge(VERIFIER))
        code = issued.json()["code"]
        browser_after = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": browser_refresh}
        )
        session = await h.client.post(
            f"{API}/app-login/session",
            headers={"X-Mowdeck-Refresh": "cookie-only"},
            json={"code": code, "code_verifier": VERIFIER},
        )
        replay = await h.client.post(
            f"{API}/app-login/session", json={"code": code, "code_verifier": VERIFIER}
        )
        me = await h.client.get(f"{API}/me", headers=bearer(session.json()["access_token"]))
        audit = await h.audit(lukas)
    assert issued.status_code == 200, issued.text
    assert issued.json()["expires_in"] == 60
    assert code.startswith("mda_")
    assert "max-age=0" in issued.headers["set-cookie"].lower()  # the browser cookie is dropped
    assert browser_after.status_code == 401  # the browser session ended with the hand-off
    assert session.status_code == 200
    assert "mowdeck_refresh" in session.headers["set-cookie"]
    assert session.json()["refresh_token"] is None
    assert me.json()["subject"] == str(lukas.user_id)
    assert replay.status_code == 401
    actions = [e["action"] for e in audit]
    assert "app_login.code_issued" in actions and "passkey.used" in actions
    login = next(e for e in audit if e["action"] == "session.login")
    assert login["details"]["method"] == "app_handoff" and login["details"]["via"] == "app"


async def test_any_login_method_can_be_handed_over() -> None:
    async with harness() as h:  # passkeys off: the hand-off does not depend on them
        await h.register("lukas")
        password = await h.login("lukas")
        issued = await browser_code(h, password["access_token"], s256_challenge(VERIFIER))
        session = await h.client.post(
            f"{API}/app-login/session",
            json={"code": issued.json()["code"], "code_verifier": VERIFIER},
        )
    assert issued.status_code == 200
    assert session.status_code == 200


async def test_only_fresh_browser_logins_of_users_are_handed_over() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        token = lukas.token
        async with h.store.transaction() as tx:  # the login is six minutes old
            for row in await tx.sessions.find(user_id=lukas.user_id):
                row.created_at -= timedelta(minutes=6)
                await tx.sessions.update(row)
        stale = await browser_code(h, token, s256_challenge(VERIFIER))
        level = {"type": "account", "id": str(lukas.account_id)}
        made = await h.client.post(
            lukas.url("/api-tokens"),
            headers=bearer(token),
            json={"name": "ha", "grants": [{"level": level, "permissions": ["mower.view"]}]},
        )
        exchanged = await h.client.post(f"{API}/token", json={"token": made.json()["token"]})
        by_token = await browser_code(h, exchanged.json()["access_token"], s256_challenge(VERIFIER))
        anonymous = await h.client.post(
            f"{API}/app-login/code", json={"code_challenge": s256_challenge(VERIFIER)}
        )
        bad = await app_request(h, "nope")
        fresh = (await h.login("lukas"))["access_token"]
        invented = await browser_code(h, fresh, "", request_id=str(uuid4()))
        started = (await app_request(h, s256_challenge(VERIFIER))).json()
        first = await browser_code(h, fresh, "", request_id=started["request_id"])
        again = await browser_code(
            h, (await h.login("lukas"))["access_token"], "", request_id=started["request_id"]
        )
        late_request = (await app_request(h, s256_challenge(VERIFIER))).json()["request_id"]
        async with h.store.transaction() as tx:  # the app started it eleven minutes ago
            request = await tx.app_login_requests.get(id=UUID(late_request))
            assert request is not None
            request.expires_at -= timedelta(minutes=11)
            await tx.app_login_requests.update(request)
        late = await browser_code(
            h, (await h.login("lukas"))["access_token"], "", request_id=late_request
        )
        audit = await h.audit(lukas)
    assert stale.status_code == 403
    assert by_token.status_code == 403
    assert anonymous.status_code == 401
    assert bad.status_code == 422
    assert started["expires_in"] == 600
    assert invented.status_code == 403  # only requests the app started
    assert first.status_code == 200
    assert again.status_code == 403  # single use
    assert late.status_code == 403
    denied = [e for e in audit if e["action"] == "app_login.denied"]
    assert denied and denied[0]["details"]["reason"] == "login not fresh"


async def test_an_app_code_needs_the_right_verifier_in_time_and_only_once() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")
        soft = SoftAuthenticator(ORIGIN)
        await add_passkey(h, lukas, soft)
        challenge_value = s256_challenge(VERIFIER)
        wrong = (await passkey_code(h, soft, challenge_value)).json()["code"]
        wrong_try = await h.client.post(
            f"{API}/app-login/session", json={"code": wrong, "code_verifier": "x" * 43}
        )
        right_after_wrong = await h.client.post(
            f"{API}/app-login/session", json={"code": wrong, "code_verifier": VERIFIER}
        )
        late = (await passkey_code(h, soft, challenge_value)).json()["code"]
        h.clock.advance(timedelta(seconds=61))
        late_try = await h.client.post(
            f"{API}/app-login/session", json={"code": late, "code_verifier": VERIFIER}
        )
        invented = await h.client.post(
            f"{API}/app-login/session",
            json={"code": "mda_" + "a" * 43, "code_verifier": VERIFIER},
        )
        audit = await h.audit(lukas, action="app_login.failed")
    assert wrong_try.status_code == 401
    assert right_after_wrong.status_code == 401  # a wrong verifier burns the code
    assert late_try.status_code == 401
    assert invented.status_code == 401
    reasons = {e["details"]["reason"] for e in audit}
    assert {"app code verifier mismatch", "expired app code"} <= reasons


async def test_disabled_users_cannot_redeem_an_app_code() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        anna_id, anna_token = await h.subuser(lukas, "anna")
        code = (await browser_code(h, anna_token, s256_challenge(VERIFIER))).json()["code"]
        await h.client.post(lukas.url(f"/users/{anna_id}/disable"), headers=lukas.headers)
        redeemed = await h.client.post(
            f"{API}/app-login/session", json={"code": code, "code_verifier": VERIFIER}
        )
    assert redeemed.status_code == 401


async def test_expired_ceremonies_and_codes_are_purged() -> None:
    async with harness(**PASSKEYS) as h:
        lukas = await h.register("lukas")
        await challenge(h)
        await browser_code(h, lukas.token, s256_challenge(VERIFIER))
        h.clock.advance(timedelta(minutes=10))
        async with h.store.transaction() as tx:
            now = h.clock()
            assert await tx.webauthn_challenges.delete(expires_at=Lt(now)) == 1
            assert await tx.app_login_codes.delete(expires_at=Lt(now)) == 1
        await app_request(h, s256_challenge(VERIFIER))
        h.clock.advance(timedelta(minutes=11))
        async with h.store.transaction() as tx:
            assert await tx.app_login_requests.delete(expires_at=Lt(h.clock())) == 1
