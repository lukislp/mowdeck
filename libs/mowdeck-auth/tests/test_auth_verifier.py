"""Token verification: the good path and every way a token must be rejected."""

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from mowdeck_auth import (
    JwtVerifier,
    SigningKey,
    StaticKeySource,
    TokenError,
    TokenIssuer,
    principal_claims,
)
from mowdeck_auth.testing import TestKeys
from mowdeck_contracts.v1 import Principal, PrincipalKind


def principal(**overrides: object) -> Principal:
    values: dict[str, object] = {
        "subject": uuid4(),
        "account_id": uuid4(),
        "kind": PrincipalKind.SUBUSER,
        "display_name": "Anna",
        "roles": ["operator"],
        "permissions": ["mower.view", "mower.control"],
        "plan": "unlimited",
        "session_id": uuid4(),
        **overrides,
    }
    return Principal.model_validate(values)


def static_verifier(keys: TestKeys, **options: object) -> JwtVerifier:
    return JwtVerifier(StaticKeySource([keys.key.public_jwk()]), **options)  # type: ignore[arg-type]


async def test_valid_token_yields_the_principal() -> None:
    keys = TestKeys()
    original = principal()
    verified = await static_verifier(keys).verify(keys.mint(original))
    assert verified.subject == original.subject
    assert verified.account_id == original.account_id
    assert verified.kind is PrincipalKind.SUBUSER
    assert verified.display_name == "Anna"
    assert verified.roles == ["operator"]
    assert verified.permissions == ["mower.view", "mower.control"]
    assert verified.plan == "unlimited"
    assert verified.session_id == original.session_id
    assert verified.token_id is not None


async def test_verify_token_reports_the_expiry() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    keys = TestKeys(clock=lambda: now)
    original = principal()
    verified = await static_verifier(keys).verify_token(
        keys.mint(original, ttl=timedelta(minutes=5))
    )
    assert verified.principal.subject == original.subject
    assert verified.expires_at == now + timedelta(minutes=5)
    assert verified.expires_at.tzinfo is UTC


async def test_api_token_principal_without_account_session_or_name() -> None:
    keys = TestKeys()
    original = Principal(subject=uuid4(), kind=PrincipalKind.API_TOKEN, permissions=["mower.view"])
    verified = await static_verifier(keys).verify(keys.mint(original))
    assert verified.kind is PrincipalKind.API_TOKEN
    assert verified.account_id is None
    assert verified.session_id is None
    assert verified.display_name is None


async def test_header_names_the_key_and_the_access_token_type() -> None:
    keys = TestKeys()
    header = jwt.get_unverified_header(keys.mint(principal()))
    assert header == {"alg": "EdDSA", "kid": keys.key.kid, "typ": "at+jwt"}


async def test_expired_token_is_rejected_but_leeway_is_honoured() -> None:
    past = datetime.now(UTC) - timedelta(minutes=20)
    keys = TestKeys(clock=lambda: past)
    expired = keys.mint(principal(), ttl=timedelta(minutes=10))
    with pytest.raises(TokenError, match="ExpiredSignature"):
        await static_verifier(keys).verify(expired)
    lenient = static_verifier(keys, leeway=timedelta(minutes=15))
    assert (await lenient.verify(expired)).kind is PrincipalKind.SUBUSER


async def test_token_from_the_future_is_rejected() -> None:
    future = datetime.now(UTC) + timedelta(minutes=5)
    keys = TestKeys(clock=lambda: future)
    with pytest.raises(TokenError, match="Immature"):
        await static_verifier(keys).verify(keys.mint(principal()))


@pytest.mark.parametrize(
    ("option", "value", "error"),
    [("audience", "someone-else", "InvalidAudience"), ("issuer", "urn:evil", "InvalidIssuer")],
)
async def test_wrong_audience_or_issuer_is_rejected(option: str, value: str, error: str) -> None:
    keys = TestKeys()
    with pytest.raises(TokenError, match=error):
        await static_verifier(keys, **{option: value}).verify(keys.mint(principal()))


async def test_unknown_kid_is_rejected() -> None:
    keys = TestKeys()
    stranger = SigningKey.generate()
    token = keys.mint(principal(), key=stranger)
    with pytest.raises(TokenError, match="unknown signing key"):
        await static_verifier(keys).verify(token)


async def test_signature_by_another_key_with_the_same_kid_is_rejected() -> None:
    keys = TestKeys()
    impostor = SigningKey(SigningKey.generate().private_key, keys.key.kid)
    with pytest.raises(TokenError, match="InvalidSignature"):
        await static_verifier(keys).verify(keys.mint(principal(), key=impostor))


def _b64(data: dict[str, object]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


async def test_tampered_payload_fails_the_signature() -> None:
    keys = TestKeys()
    token = keys.mint(principal())
    header, _, signature = token.split(".")
    claims = jwt.decode(token, options={"verify_signature": False})
    claims["acc"] = str(uuid4())  # move the token to another account
    forged = f"{header}.{_b64(claims)}.{signature}"
    with pytest.raises(TokenError, match="InvalidSignature"):
        await static_verifier(keys).verify(forged)


async def test_alg_none_is_rejected() -> None:
    keys = TestKeys()
    claims = jwt.decode(keys.mint(principal()), options={"verify_signature": False})
    unsigned = f"{_b64({'alg': 'none', 'typ': 'at+jwt', 'kid': keys.key.kid})}.{_b64(claims)}."
    with pytest.raises(TokenError, match="not allowed"):
        await static_verifier(keys).verify(unsigned)


async def test_hs256_with_the_public_key_as_secret_is_rejected() -> None:
    keys = TestKeys()
    claims = jwt.decode(keys.mint(principal()), options={"verify_signature": False})
    secret = keys.key.public_jwk()["x"].encode()
    forged = jwt.encode(
        claims, secret, algorithm="HS256", headers={"kid": keys.key.kid, "typ": "at+jwt"}
    )
    with pytest.raises(TokenError, match="not allowed"):
        await static_verifier(keys).verify(forged)


def test_unsafe_algorithms_cannot_be_configured() -> None:
    with pytest.raises(ValueError, match="unsafe"):
        JwtVerifier(StaticKeySource(), algorithms=["EdDSA", "HS256"])
    with pytest.raises(ValueError, match="unsafe"):
        JwtVerifier(StaticKeySource(), algorithms=["none"])


async def test_wrong_type_missing_kid_and_garbage_are_rejected() -> None:
    keys = TestKeys()
    verifier = static_verifier(keys)
    claims = jwt.decode(keys.mint(principal()), options={"verify_signature": False})
    id_token = jwt.encode(
        claims, keys.key.private_key, algorithm="EdDSA", headers={"kid": keys.key.kid}
    )
    with pytest.raises(TokenError, match="not an access token"):
        await verifier.verify(id_token)
    no_kid = jwt.encode(claims, keys.key.private_key, algorithm="EdDSA", headers={"typ": "at+jwt"})
    with pytest.raises(TokenError, match="no kid"):
        await verifier.verify(no_kid)
    for garbage in ("", "x" * 9000, "not-a-jwt", "a.b.c"):
        with pytest.raises(TokenError):
            await verifier.verify(garbage)


async def test_missing_required_claim_is_rejected() -> None:
    keys = TestKeys()
    now = int(datetime.now(UTC).timestamp())
    claims = {"sub": str(uuid4()), "iss": keys.issuer, "aud": keys.audience, "iat": now}
    claims |= {"exp": now + 600, "jti": str(uuid4())}  # no "kind"
    token = jwt.encode(
        claims,
        keys.key.private_key,
        algorithm="EdDSA",
        headers={"kid": keys.key.kid, "typ": "at+jwt"},
    )
    with pytest.raises(TokenError, match="MissingRequiredClaim"):
        await static_verifier(keys).verify(token)


@pytest.mark.parametrize(
    "bad",
    [{"kind": "wizard"}, {"sub": "not-a-uuid"}, {"perms": "mower.view"}, {"acc": 7}],
)
async def test_malformed_claims_are_rejected(bad: dict[str, object]) -> None:
    keys = TestKeys()
    with pytest.raises(TokenError, match="invalid claims"):
        await static_verifier(keys).verify(keys.mint(principal(), extra_claims=bad))


async def test_non_okp_key_with_a_known_kid_is_rejected() -> None:
    keys = TestKeys()
    rsa_jwk = {
        "kty": "RSA",
        "kid": keys.key.kid,
        "n": (
            "sXchDaQebHnPiGvyDOAT4saGEUetSyo9MKLOoWFsueri23bOdgWp4Dy1WlUzewbgBHod5pcM9H95GQRV"
            "3JDXboIRROSBigeC5yjU1hGzHHyXss8UDprecbAYxknTcQkhslANGRUZmdTOQ5qTRsLAt6BTYuyvVRdh"
            "S8exSZEy_c4gs_7svlJJQ4H9_NxsiIoLwAEk7-Q3UXERGYw_75IDrGA84-lA_-Ct4eTlXHBIY2EaV7t7"
            "LjJaynVJCpkv4LKjTTAumiGUIuQhrNhZLuF_RJLqHpM2kgWFLU7-VTdL1VbC2tejvcI2BlMkEpk1BzBZ"
            "I0KQB0GaDWFLN-aEAw3vRw"
        ),
        "e": "AQAB",
        "alg": "RS256",
        "use": "sig",
    }
    verifier = JwtVerifier(StaticKeySource([rsa_jwk]))
    with pytest.raises(TokenError, match="not an Ed25519 key"):
        await verifier.verify(keys.mint(principal()))


def test_issuer_rejects_a_non_positive_ttl_and_reports_expiry() -> None:
    key = SigningKey.generate()
    with pytest.raises(ValueError, match="positive"):
        TokenIssuer(lambda: key, ttl=timedelta(0))
    issued = TokenIssuer(lambda: key, ttl=timedelta(minutes=10)).issue(principal())
    assert issued.expires_in == 600
    assert issued.kid == key.kid


def test_principal_claims_omit_empty_optional_fields() -> None:
    bare = Principal(subject=uuid4(), kind=PrincipalKind.SERVICE)
    assert set(principal_claims(bare)) == {"sub", "kind", "roles", "perms"}
    long_name = principal(display_name="x" * 150)
    assert len(principal_claims(long_name)["name"]) == 64
