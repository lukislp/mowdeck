"""Sign in with Apple (ADR 0052): the client secret, the ID token validation, linking, the login,
unlinking, refusals and the settings.

Apple is the fake of ``apple_support`` behind the real ``AppleOAuthClient`` (httpx with a mock
transport), so the requests MowDeck sends to Apple are checked as Apple would. The round trip
itself (state, browser binding, single use, expiry) is the class shared with GitHub and tested
in ``test_identity_github``; here it is checked only where Apple differs.
"""

import logging
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from apple_support import (
    CALLBACK,
    ISSUER,
    KEY_ID,
    SERVICES_ID,
    TEAM_ID,
    TOKEN,
    FakeApple,
    new_ec_key,
    pem,
    rsa_key,
    s256,
    settings,
    write_key,
)
from cryptography.hazmat.primitives import serialization
from identity_support import API, PASSWORD, Account, Harness, harness, make_settings

from identity.adapters.apple import AppleOAuthClient, load_signing_key
from identity.application.ports import ProviderRejected, ProviderUnavailable
from identity.domain.models import oauth_callback_url

SUB = "001234.0a1b2c3d4e5f60718293a4b5c6d7e8f9.0420"
OTHER_SUB = "009999.ffffffffffffffffffffffffffffffff.1111"


@pytest.fixture
def fake_apple(tmp_path: Path) -> tuple[FakeApple, dict[str, Any]]:
    """The fake with its client key, and the settings that turn Apple login on against it."""
    fake = FakeApple()
    return fake, settings(write_key(tmp_path, fake.client_key))


def where(response: httpx.Response) -> tuple[str, dict[str, str]]:
    assert response.status_code == 303, response.text
    parts = urlsplit(response.headers["location"])
    assert not parts.scheme and not parts.netloc  # always a local page
    path = parts.path + (f"#{parts.fragment}" if parts.fragment else "")
    return path, {k: v[0] for k, v in parse_qs(parts.query).items()}


async def callback(h: Harness, query: dict[str, str]) -> httpx.Response:
    return await h.client.get(f"{API}/oauth/apple/callback", params=query)


async def link(h: Harness, fake: FakeApple, who: Account, sub: str) -> httpx.Response:
    started = await h.client.post(
        f"{API}/me/external-identities/apple/link",
        headers=who.headers,
        json={"password": PASSWORD},
    )
    assert started.status_code == 200, started.text
    return await callback(h, fake.authorize(started.json()["authorize_url"], sub))


async def start_login(h: Harness) -> httpx.Response:
    started = await h.client.post(f"{API}/oauth/apple/start")
    assert started.status_code == 200, started.text
    return started


async def apple_login(
    h: Harness, fake: FakeApple, sub: str, return_query: str | None = None
) -> httpx.Response:
    body = {"return_query": return_query} if return_query is not None else None
    started = await h.client.post(f"{API}/oauth/apple/start", json=body)
    assert started.status_code == 200, started.text
    return await callback(h, fake.authorize(started.json()["authorize_url"], sub))


# --- settings ------------------------------------------------------------------------------------


def test_callback_url_of_apple() -> None:
    assert oauth_callback_url("https://mowdeck.example.org/", "apple") == CALLBACK


def test_settings_turn_apple_on_only_when_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    key = new_ec_key()
    good = settings(write_key(tmp_path, key))
    assert make_settings().apple_login() is None
    on = make_settings(**good).apple_login()
    assert on is not None and on.redirect_uri == CALLBACK and on.services_id == SERVICES_ID
    assert on.pkce is True
    assert pem(key).splitlines()[1] not in repr(on)
    (tmp_path / "bad.p8").write_text("not a key", encoding="utf-8")
    rsa_pem = rsa_key().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (tmp_path / "rsa.p8").write_bytes(rsa_pem)
    incomplete = [
        {**good, "identity_apple_services_id": ""},
        {**good, "identity_apple_services_id": "not a services id"},
        {**good, "identity_apple_team_id": ""},
        {**good, "identity_apple_team_id": "short"},
        {**good, "identity_apple_key_id": "lower12345"},
        {**good, "identity_apple_private_key_file": ""},
        {**good, "identity_apple_private_key_file": str(tmp_path / "missing.p8")},
        {**good, "identity_apple_private_key_file": str(tmp_path / "bad.p8")},
        {**good, "identity_apple_private_key_file": str(tmp_path / "rsa.p8")},
        {**good, "identity_public_url": ""},
        {**good, "identity_public_url": "http://mowdeck.lan"},
    ]
    for values in incomplete:
        assert make_settings(**values).apple_login() is None
    captured = capsys.readouterr()
    text = captured.out + captured.err + caplog.text
    assert "Apple login is on" in text and CALLBACK in text
    assert text.count("Apple login is off") == len(incomplete)
    assert "BEGIN PRIVATE KEY" not in text and "not a key" not in text


def test_the_private_key_must_be_an_ec_p256_pem() -> None:
    assert load_signing_key(pem(new_ec_key())) is not None
    with pytest.raises(ValueError, match="readable"):
        load_signing_key("garbage")


# --- the client secret ---------------------------------------------------------------------------


def client(
    fake: FakeApple, *, clock: Callable[[], float] = time.time, **kw: Any
) -> AppleOAuthClient:
    return AppleOAuthClient(
        services_id=SERVICES_ID,
        team_id=TEAM_ID,
        key_id=KEY_ID,
        private_key=fake.client_key,
        authorize_url="https://apple.test/auth/authorize",
        token_url=TOKEN,
        keys_url="https://apple.test/auth/keys",
        transport=fake.transport,
        clock=clock,
        **kw,
    )


def test_the_client_secret_is_a_short_lived_es256_jwt_of_the_team() -> None:
    now = [1_800_000_000.0]
    fake = FakeApple()
    apple = client(fake, clock=lambda: now[0])
    token = apple.client_secret()
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(
        token,
        fake.client_key.public_key(),
        algorithms=["ES256"],
        audience=ISSUER,
        options={"verify_exp": False, "verify_iat": False},
    )
    assert header == {"alg": "ES256", "kid": KEY_ID, "typ": "JWT"}
    assert claims == {
        "iss": TEAM_ID,
        "iat": 1_800_000_000,
        "exp": 1_800_003_600,
        "aud": ISSUER,
        "sub": SERVICES_ID,
    }
    # Cached while valid, replaced shortly before it ends.
    now[0] += 3000
    assert apple.client_secret() == token
    now[0] += 400
    renewed = apple.client_secret()
    assert renewed != token
    assert (
        jwt.decode(
            renewed,
            fake.client_key.public_key(),
            algorithms=["ES256"],
            audience=ISSUER,
            options={"verify_exp": False, "verify_iat": False},
        )["exp"]
        == int(now[0]) + 3600
    )
    # A signature of another key does not verify.
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            token,
            new_ec_key().public_key(),
            algorithms=["ES256"],
            audience=ISSUER,
            options={"verify_exp": False, "verify_iat": False},
        )


def test_the_authorization_url_asks_for_nothing_but_the_subject() -> None:
    apple = client(FakeApple())
    challenge = s256("v" * 43)
    url = apple.authorization_url(state="s" * 43, code_challenge=challenge, redirect_uri=CALLBACK)
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert query["response_mode"] == "query" and query["response_type"] == "code"
    assert "scope" not in query
    assert query["nonce"] == challenge and query["code_challenge"] == challenge
    assert query["code_challenge_method"] == "S256"
    off = client(FakeApple(), pkce=False).authorization_url(
        state="s" * 43, code_challenge=challenge, redirect_uri=CALLBACK
    )
    assert "code_challenge" not in off and "nonce=" in off


# --- the ID token --------------------------------------------------------------------------------


async def redeem(fake: FakeApple, apple: AppleOAuthClient | None = None, sub: str = SUB) -> Any:
    """A whole round trip against the fake, without the authenticator around it."""
    apple = apple or client(fake)
    verifier = secrets.token_urlsafe(32)
    url = apple.authorization_url(
        state=secrets.token_urlsafe(32), code_challenge=s256(verifier), redirect_uri=CALLBACK
    )
    query = fake.authorize(url, sub)
    return await apple.profile(code=query["code"], code_verifier=verifier, redirect_uri=CALLBACK)


async def test_a_valid_id_token_gives_the_subject_and_a_neutral_label() -> None:
    fake = FakeApple()
    profile = await redeem(fake)
    assert profile.subject == SUB
    assert profile.display_name == "Apple ID"  # never the (relay) e-mail address in the token
    assert fake.form_seen[0]["grant_type"] == "authorization_code"
    assert fake.form_seen[0]["code_verifier"]
    assert fake.secrets_seen[0]["claims"]["sub"] == SERVICES_ID


@pytest.mark.parametrize(
    ("claims", "why"),
    [
        ({"aud": "someone.else"}, "audience"),
        ({"iss": "https://evil.example"}, "issuer"),
        ({"nonce": "x" * 43}, "nonce"),
        ({"nonce": None}, "no nonce"),
        ({"exp": int(time.time()) - 3600}, "expired"),
        ({"iat": int(time.time()) + 3600}, "issued in the future"),
        ({"sub": None}, "no sub"),
        ({"sub": ""}, "empty sub"),
        ({"sub": 12345}, "numeric sub"),
        ({"exp": None}, "no exp"),
    ],
)
async def test_an_id_token_with_wrong_claims_is_rejected(claims: dict[str, Any], why: str) -> None:
    fake = FakeApple(claims=claims)
    with pytest.raises(ProviderRejected):
        await redeem(fake)


async def test_an_id_token_with_a_bad_signature_is_rejected() -> None:
    forged = FakeApple(forged_key=rsa_key(7))  # published kid, another key
    with pytest.raises(ProviderRejected, match="InvalidSignature"):
        await redeem(forged)
    unknown_kid = FakeApple(forged_kid="NOSUCHKEY")
    with pytest.raises(ProviderRejected, match="unknown signing key"):
        await redeem(unknown_kid)


async def test_an_unsigned_or_symmetric_id_token_is_rejected() -> None:
    fake = FakeApple()
    apple = client(fake)
    verifier = secrets.token_urlsafe(32)
    url = apple.authorization_url(
        state="s" * 43, code_challenge=s256(verifier), redirect_uri=CALLBACK
    )
    nonce = parse_qs(urlsplit(url).query)["nonce"][0]
    claims = {
        "iss": ISSUER,
        "aud": SERVICES_ID,
        "sub": SUB,
        "nonce": nonce,
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    for token in (
        jwt.encode(claims, "", algorithm="none", headers={"kid": fake.kid}),
        jwt.encode(claims, "secret" * 8, algorithm="HS256", headers={"kid": fake.kid}),
        "not-a-jwt",
    ):
        async with apple._client() as http:
            with pytest.raises(ProviderRejected):
                await apple._validate(http, token, nonce)


async def test_signing_keys_are_cached_and_re_read_for_an_unknown_kid() -> None:
    now = [1_800_000_000.0]
    fake = FakeApple()
    apple = client(fake, clock=lambda: now[0])
    await redeem(fake, apple)
    await redeem(fake, apple)
    assert fake.requests.count("GET https://apple.test/auth/keys") == 1
    # Apple rotates its key: the new kid is unknown, the set is read again (after the minimum
    # interval) and the old key is gone.
    fake.rotate()
    now[0] += 120
    assert (await redeem(fake, apple)).subject == SUB
    assert fake.requests.count("GET https://apple.test/auth/keys") == 2
    # A forged token with an unknown kid cannot make MowDeck read the key set again and again.
    fake.forged_kid = "NOSUCHKEY"
    for _ in range(3):
        with pytest.raises(ProviderRejected):
            await redeem(fake, apple)
    assert fake.requests.count("GET https://apple.test/auth/keys") == 2
    now[0] += 61
    with pytest.raises(ProviderRejected):
        await redeem(fake, apple)
    assert fake.requests.count("GET https://apple.test/auth/keys") == 3
    # ...and the keys are re-read daily at the latest.
    fake.forged_kid = None
    now[0] += 25 * 3600
    await redeem(fake, apple)
    assert fake.requests.count("GET https://apple.test/auth/keys") == 4


async def test_apple_errors_are_told_apart() -> None:
    cases = {
        "token_5xx": ProviderUnavailable,
        "keys_5xx": ProviderUnavailable,
        "network": ProviderUnavailable,
        "invalid_client": ProviderUnavailable,  # the installation's key or ids are wrong
        "no_id_token": ProviderRejected,
    }
    for fail, expected in cases.items():
        with pytest.raises(expected):
            await redeem(FakeApple(fail={fail}))
    # A code Apple does not know (or already redeemed) is the attempt's fault.
    fake = FakeApple()
    apple = client(fake)
    with pytest.raises(ProviderRejected, match="invalid_grant"):
        await apple.profile(code="nope", code_verifier="v" * 43, redirect_uri=CALLBACK)


# --- link, login, unlink -------------------------------------------------------------------------


async def test_apple_is_off_unless_configured() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        methods = (await h.client.get(f"{API}/login-methods")).json()
        start = await h.client.post(f"{API}/oauth/apple/start")
        cb = await callback(h, {"state": "x", "code": "y"})
        linking = await h.client.post(
            f"{API}/me/external-identities/apple/link",
            headers=lukas.headers,
            json={"password": PASSWORD},
        )
    assert methods["apple"] is False
    assert start.status_code == 404 and cb.status_code == 404 and linking.status_code == 404


async def test_link_then_sign_in_with_apple_then_unlink(
    fake_apple: tuple[FakeApple, dict[str, Any]],
) -> None:
    fake, conf = fake_apple
    async with harness(apple_transport=fake.transport, **conf) as h:
        lukas = await h.register("lukas")
        methods = (await h.client.get(f"{API}/login-methods")).json()
        linked = await link(h, fake, lukas, SUB)
        links = (await h.client.get(f"{API}/me/external-identities", headers=lukas.headers)).json()
        h.client.cookies.clear()
        login = await apple_login(h, fake, SUB, "next=/schedules")
        refreshed = await h.client.post(
            f"{API}/session/refresh", headers={"X-Requested-With": "test"}, json=None
        )
        unlinked = await h.client.delete(
            f"{API}/me/external-identities/apple", headers=lukas.headers
        )
        h.client.cookies.clear()
        refused = await apple_login(h, fake, SUB)
        password = await h.client.post(
            f"{API}/session", json={"username": "lukas", "password": PASSWORD}
        )
        audit = await h.audit(lukas)
    assert methods["apple"] is True and methods["github"] is False
    assert where(linked) == ("/settings#security", {"linked": "apple"})
    assert [(x["provider"], x["display_name"]) for x in links] == [("apple", "Apple ID")]
    assert where(login) == ("/login", {"next": "/schedules", "signed_in_with": "apple"})
    cookie = login.headers["set-cookie"]
    assert "mowdeck_refresh=" in cookie and "HttpOnly" in cookie
    assert refreshed.status_code == 200, refreshed.text
    assert unlinked.status_code == 204
    assert where(refused)[1]["sign_in_error"] == "denied"
    assert password.status_code == 200  # the password always stays
    entry = next(
        e for e in audit if e["action"] == "session.login" and e["details"]["method"] == "apple"
    )
    assert entry["details"]["provider_subject"] == SUB
    # One token request per round trip; no token revoked (nothing of Apple's is kept).
    assert fake.requests.count(f"POST {TOKEN}") == 3
    assert not any("revoke" in r for r in fake.requests)
    async with h.store.transaction() as tx:
        assert await tx.external_identities.count() == 0


async def test_the_e_mail_of_the_id_token_is_never_used_or_stored(
    fake_apple: tuple[FakeApple, dict[str, Any]],
) -> None:
    fake, conf = fake_apple
    fake.claims = {"email": "lukas@example.org", "email_verified": True}
    async with harness(apple_transport=fake.transport, **conf) as h:
        response = await h.client.post(
            f"{API}/register",
            json={
                "username": "lukas",
                "password": PASSWORD,
                "account_name": "home",
                "email": "lukas@example.org",
            },
        )
        # An Apple account with the same (verified) address as the MowDeck user.
        login = await apple_login(h, fake, SUB)
        async with h.store.transaction() as tx:
            links = await tx.external_identities.count()
    assert response.status_code == 201
    assert where(login)[1]["sign_in_error"] == "denied"
    assert links == 0


async def test_an_unknown_apple_account_is_refused_and_never_registered(
    fake_apple: tuple[FakeApple, dict[str, Any]],
) -> None:
    fake, conf = fake_apple
    async with harness(apple_transport=fake.transport, **conf) as h:
        await h.register("lukas")
        login = await apple_login(h, fake, OTHER_SUB, "next=/mowers")
        async with h.store.transaction() as tx:
            users = await tx.users.count()
    path, query = where(login)
    assert (path, query["sign_in_error"], query["provider"]) == ("/login", "denied", "apple")
    assert "mowdeck_refresh" not in login.headers.get("set-cookie", "")
    assert users == 1


async def test_an_apple_account_belongs_to_one_user(
    fake_apple: tuple[FakeApple, dict[str, Any]],
) -> None:
    fake, conf = fake_apple
    async with harness(apple_transport=fake.transport, **conf) as h:
        lukas = await h.register("lukas")
        anna = await h.register("anna")
        first = await link(h, fake, lukas, SUB)
        second = await link(h, fake, anna, SUB)
    assert where(first)[1] == {"linked": "apple"}
    assert "link_error" in where(second)[1]


async def test_a_forged_id_token_signs_nobody_in(
    fake_apple: tuple[FakeApple, dict[str, Any]],
) -> None:
    fake, conf = fake_apple
    async with harness(apple_transport=fake.transport, **conf) as h:
        lukas = await h.register("lukas")
        await link(h, fake, lukas, SUB)
        h.client.cookies.clear()
        fake.forged_key = rsa_key(9)
        forged = await apple_login(h, fake, SUB)
        fake.forged_key = None
        fake.claims = {"nonce": "x" * 43}
        replayed = await apple_login(h, fake, SUB)
    for response in (forged, replayed):
        assert where(response)[1]["sign_in_error"] == "denied"
        assert "mowdeck_refresh=" not in response.headers.get("set-cookie", "")


async def test_apple_cancel_and_failures_end_the_round_trip(
    fake_apple: tuple[FakeApple, dict[str, Any]],
) -> None:
    fake, conf = fake_apple
    async with harness(apple_transport=fake.transport, **conf) as h:
        lukas = await h.register("lukas")
        await link(h, fake, lukas, SUB)
        started = await start_login(h)
        cancelled = await callback(h, fake.cancel(started.json()["authorize_url"]))
        fake.fail = {"token_5xx"}
        down = await apple_login(h, fake, SUB)
        fake.fail = {"invalid_client"}
        misconfigured = await apple_login(h, fake, SUB)
        fake.fail = set()
        replay_started = await start_login(h)
        query = fake.authorize(replay_started.json()["authorize_url"], SUB)
        first = await callback(h, query)
        replay = await callback(h, query)
        h.client.cookies.clear()
        other_browser_started = await start_login(h)
        query = fake.authorize(other_browser_started.json()["authorize_url"], SUB)
        h.client.cookies.clear()
        other_browser = await callback(h, query)
    assert where(cancelled)[1]["sign_in_error"] == "cancelled"
    assert where(down)[1]["sign_in_error"] == "unavailable"
    assert where(misconfigured)[1]["sign_in_error"] == "unavailable"
    assert where(first)[1]["signed_in_with"] == "apple"
    assert where(replay)[1]["sign_in_error"] == "expired"
    assert where(other_browser)[1]["sign_in_error"] == "expired"


async def test_pkce_off_leaves_the_verifier_out(tmp_path: Path) -> None:
    fake = FakeApple(pkce=False)
    conf = {**settings(write_key(tmp_path, fake.client_key)), "identity_apple_pkce": False}
    async with harness(apple_transport=fake.transport, **conf) as h:
        lukas = await h.register("lukas")
        linked = await link(h, fake, lukas, SUB)
        login = await apple_login(h, fake, SUB)
    assert where(linked)[1] == {"linked": "apple"}
    assert where(login)[1]["signed_in_with"] == "apple"
    assert all("code_verifier" not in form for form in fake.form_seen)
