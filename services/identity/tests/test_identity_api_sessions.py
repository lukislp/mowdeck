"""Registration, login, me, refresh rotation and reuse, logout, JWKS, rate limits."""

from datetime import timedelta
from uuid import UUID

import jwt
from identity_support import API, PASSWORD, bearer, harness
from mowdeck_auth import StaticKeySource
from mowdeck_auth.verifier import JwtVerifier
from mowdeck_contracts.v1 import MODULE_DESCRIPTOR_PATH, PrincipalKind


async def test_register_login_me() -> None:
    async with harness() as h:
        lukas = await h.register("lukas", account_name="Garden")
        me = await h.client.get(f"{API}/me", headers=lukas.headers)
        account = await h.client.get(lukas.url(), headers=lukas.headers)
    assert me.status_code == 200
    body = me.json()
    assert body["subject"] == str(lukas.user_id)
    assert body["account_id"] == str(lukas.account_id)
    assert body["kind"] == "user"
    assert body["roles"] == ["owner"]
    assert {"mower.view", "users.manage", "billing.manage", "device_gateway.view"} <= set(
        body["permissions"]
    )
    assert body["memberships"][0]["is_owner"] is True
    assert body["session_id"] is not None
    assert account.json()["name"] == "Garden"
    assert account.json()["status"] == "active"


async def test_token_claims_and_jwks() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        jwks = await h.client.get("/.well-known/jwks.json")
        routed = await h.client.get(f"{API}/.well-known/jwks.json")
        descriptor = await h.client.get(MODULE_DESCRIPTOR_PATH)
    assert jwks.status_code == 200
    assert jwks.headers["cache-control"] == "public, max-age=300"
    assert jwks.json() == routed.json()
    keys = jwks.json()["keys"]
    assert [k["kty"] for k in keys] == ["OKP"]
    header = jwt.get_unverified_header(lukas.token)
    assert (
        header["alg"] == "EdDSA" and header["typ"] == "at+jwt" and header["kid"] == keys[0]["kid"]
    )
    verifier = JwtVerifier(StaticKeySource(jwks.json()))
    principal = await verifier.verify(lukas.token)
    assert principal.kind is PrincipalKind.USER
    assert principal.account_id == lukas.account_id
    claims = jwt.decode(lukas.token, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 600
    assert claims["acc"] == str(lukas.account_id)
    assert len(lukas.token) < 1024
    assert {p["key"] for p in descriptor.json()["permissions"]} == {
        "users.manage",
        "audit.view",
        "billing.manage",
    }


async def test_wrong_password_unknown_user_and_audit() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        wrong = await h.client.post(
            f"{API}/session", json={"username": "lukas", "password": "nope-nope-nope"}
        )
        unknown = await h.client.post(
            f"{API}/session", json={"username": "ghost", "password": PASSWORD}
        )
        entries = await h.audit(lukas, action="session.login_failed")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"] == "invalid credentials"
    assert {e["details"]["reason"] for e in entries} <= {"wrong password"}
    assert entries and "nope" not in str(entries)


async def test_username_throttle_answers_429() -> None:
    async with harness(identity_login_max_failures=3) as h:
        await h.register("lukas")
        codes = [
            (
                await h.client.post(
                    f"{API}/session", json={"username": "lukas", "password": "wrong-password-x"}
                )
            ).status_code
            for _ in range(4)
        ]
        blocked = await h.client.post(
            f"{API}/session", json={"username": "LUKAS", "password": PASSWORD}
        )
    assert codes == [401, 401, 401, 429]
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert blocked.json()["type"] == "urn:mowdeck:problem:too-many-requests"


async def test_ip_rate_limit_answers_429() -> None:
    async with harness(identity_login_ip_burst=2, identity_login_ip_per_minute=1) as h:
        codes = [
            (
                await h.client.post(
                    f"{API}/session", json={"username": "x-user", "password": PASSWORD}
                )
            ).status_code
            for _ in range(3)
        ]
    assert codes == [401, 401, 429]


async def test_forwarded_for_is_used_only_when_trusted() -> None:
    async with harness(identity_login_ip_burst=1, identity_trust_forwarded_for=True) as h:
        first = await h.client.post(
            f"{API}/session",
            json={"username": "x-user", "password": PASSWORD},
            headers={"X-Forwarded-For": "10.0.0.1"},
        )
        other_ip = await h.client.post(
            f"{API}/session",
            json={"username": "x-user", "password": PASSWORD},
            headers={"X-Forwarded-For": "10.0.0.2"},
        )
        same_ip = await h.client.post(
            f"{API}/session",
            json={"username": "x-user", "password": PASSWORD},
            headers={"X-Forwarded-For": "10.0.0.1, 1.2.3.4"},
        )
    assert (first.status_code, other_ip.status_code, same_ip.status_code) == (401, 401, 429)


async def test_forwarded_for_behind_an_appending_proxy_cannot_be_forged() -> None:
    """MOWDECK_CLIENT_IP_DEPTH=1 (nginx appends, Traefik appends nginx): the entry the proxy
    wrote counts, not what the client put in front of it."""
    async with harness(
        identity_login_ip_burst=1, identity_trust_forwarded_for=True, client_ip_depth=1
    ) as h:
        codes = []
        for chain in (
            "198.51.100.7, 203.0.113.1, 192.0.2.10",
            "198.51.100.8, 203.0.113.1, 192.0.2.10",
            "203.0.113.2, 192.0.2.10",
        ):
            response = await h.client.post(
                f"{API}/session",
                json={"username": "x-user", "password": PASSWORD},
                headers={"X-Forwarded-For": chain},
            )
            codes.append(response.status_code)
    assert codes == [401, 429, 401]


async def test_ipv6_clients_share_the_login_limit_of_their_64() -> None:
    """One IPv6 connection owns a whole /64: another address of it is the same client for the
    login limit, the next /64 is another client."""
    async with harness(identity_login_ip_burst=1, identity_trust_forwarded_for=True) as h:
        codes = []
        for address in ("2001:db8:1:2::1", "2001:db8:1:2:abcd::9", "2001:db8:1:3::1"):
            response = await h.client.post(
                f"{API}/session",
                json={"username": "x-user", "password": PASSWORD},
                headers={"X-Forwarded-For": address},
            )
            codes.append(response.status_code)
    assert codes == [401, 429, 401]


async def test_refresh_rotates_and_reuse_revokes_the_family() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        first = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        assert first.status_code == 200
        rotated = first.json()["refresh_token"]
        assert rotated != lukas.refresh_token
        assert (
            first.json()["session_id"]
            == jwt.decode(lukas.token, options={"verify_signature": False})["sid"]
        )
        h.clock.advance(timedelta(seconds=11))  # past the reuse grace
        reuse = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        after_reuse = await h.client.post(f"{API}/session/refresh", json={"refresh_token": rotated})
        entries = await h.audit(lukas, action="session.refresh_reuse")
    assert reuse.status_code == 401
    assert after_reuse.status_code == 401  # the whole family is revoked
    assert len(entries) == 1


async def test_refresh_with_cookie_needs_the_csrf_header() -> None:
    async with harness() as h:
        await h.register("lukas")
        cookie = h.client.cookies.get("mowdeck_refresh")
        assert cookie
        without = await h.client.post(f"{API}/session/refresh")
        with_header = await h.client.post(
            f"{API}/session/refresh", headers={"X-Requested-With": "fetch"}
        )
        h.client.cookies.clear()
        nothing = await h.client.post(f"{API}/session/refresh")
    assert without.status_code == 400
    assert with_header.status_code == 200
    assert nothing.status_code == 422


async def test_refresh_expires_and_logout_ends_the_session() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        other = await h.login("lukas")
        out = await h.client.post(
            f"{API}/session/logout", json={"refresh_token": other["refresh_token"]}
        )
        again = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": other["refresh_token"]}
        )
        unknown = await h.client.post(
            f"{API}/session/logout", json={"refresh_token": "mdr_unknown"}
        )
        h.clock.advance(timedelta(days=8))
        expired = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
    assert out.status_code == 204
    assert again.status_code == 401
    assert unknown.status_code == 204
    assert expired.status_code == 401


async def test_logout_all_and_password_change() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        second = await h.login("lukas")
        bad = await h.client.put(
            f"{API}/me/password",
            headers=lukas.headers,
            json={"current_password": "wrong-one-x", "new_password": "a new long password"},
        )
        weak = await h.client.put(
            f"{API}/me/password",
            headers=lukas.headers,
            json={"current_password": PASSWORD, "new_password": "short"},
        )
        changed = await h.client.put(
            f"{API}/me/password",
            headers=lukas.headers,
            json={"current_password": PASSWORD, "new_password": "a new long password"},
        )
        own = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        other = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": second["refresh_token"]}
        )
        relogin = await h.login("lukas", "a new long password")
        everything = await h.client.post(
            f"{API}/session/logout-all", headers=bearer(relogin["access_token"])
        )
        gone = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": relogin["refresh_token"]}
        )
    assert bad.status_code == 401
    assert weak.status_code == 422
    assert changed.status_code == 204
    assert own.status_code == 200  # the session that changed the password stays
    assert other.status_code == 401
    assert everything.status_code == 204
    assert gone.status_code == 401


async def test_protected_endpoints_need_a_valid_token() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        missing = await h.client.get(f"{API}/me")
        garbage = await h.client.get(f"{API}/me", headers=bearer("garbage"))
        tampered_token = lukas.token[:-4] + ("AAAA" if not lukas.token.endswith("AAAA") else "BBBB")
        tampered = await h.client.get(f"{API}/me", headers=bearer(tampered_token))
    assert missing.status_code == garbage.status_code == tampered.status_code == 401
    assert garbage.headers["www-authenticate"] == 'Bearer error="invalid_token"'


async def test_registration_validation_and_conflicts() -> None:
    async with harness() as h:
        await h.register("lukas")
        taken = await h.client.post(
            f"{API}/register", json={"username": "Lukas", "password": PASSWORD, "account_name": "x"}
        )
        weak = await h.client.post(
            f"{API}/register",
            json={"username": "anna", "password": "1234567890", "account_name": "x"},
        )
        bad_name = await h.client.post(
            f"{API}/register", json={"username": "a b", "password": PASSWORD, "account_name": "x"}
        )
        extra = await h.client.post(
            f"{API}/register",
            json={"username": "anna", "password": PASSWORD, "account_name": "x", "role": "admin"},
        )
        with_mail = await h.client.post(
            f"{API}/register",
            json={
                "username": "eve",
                "password": PASSWORD,
                "account_name": "x",
                "email": "Eve@Example.org",
            },
        )
        mail_taken = await h.client.post(
            f"{API}/register",
            json={
                "username": "eve2",
                "password": PASSWORD,
                "account_name": "x",
                "email": "eve@example.org",
            },
        )
    assert taken.status_code == 409
    assert weak.status_code == 422
    assert bad_name.status_code == 422
    assert extra.status_code == 422
    assert with_mail.status_code == 201
    assert mail_taken.status_code == 409


async def test_closed_registration_allows_only_the_first_account() -> None:
    async with harness(identity_registration="closed") as h:
        first = await h.client.post(
            f"{API}/register",
            json={"username": "lukas", "password": PASSWORD, "account_name": "Home"},
        )
        second = await h.client.post(
            f"{API}/register", json={"username": "bob", "password": PASSWORD, "account_name": "Bob"}
        )
    assert first.status_code == 201
    assert second.status_code == 403


async def test_registration_publishes_events() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        subjects = [m.subject for m in h.bus.published]
    assert "event.identity.account.created" in subjects
    assert "event.identity.user.created" in subjects
    assert "event.identity.grants.changed" in subjects
    assert UUID(str(lukas.account_id))
