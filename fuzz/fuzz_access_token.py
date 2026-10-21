"""Bearer credentials: access tokens (libs/mowdeck-auth), API tokens and JWKS documents.

Every service verifies the ``Authorization`` header of every request locally, and Identity
resolves ``mdk_`` API tokens. Properties:

* ``JwtVerifier.verify_token`` raises nothing but ``TokenError`` for any string, and for any
  claims signed with a trusted key (the path behind the signature check); an accepted token
  yields the principal of its claims;
* ``parse_api_token`` accepts exactly ``mdk_<32 lower-case hex>_<secret of at least 32
  characters>``, one spelling per token;
* ``parse_jwks`` of any JSON document (what ``JwksCache.refresh`` passes it) raises nothing but
  the ``ValueError`` the cache handles.
"""

import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import atheris

with atheris.instrument_imports():
    import jwt
    from mowdeck_auth.jwks import StaticKeySource, parse_jwks
    from mowdeck_auth.keys import SigningKey
    from mowdeck_auth.settings import DEFAULT_AUDIENCE, DEFAULT_ISSUER
    from mowdeck_auth.verifier import JwtVerifier, TokenError

    from identity.domain import credentials

from harness import Input, run_sync

KEY = SigningKey.from_raw(bytes(range(32)))
VERIFIER = JwtVerifier(StaticKeySource([KEY.public_jwk()]))


def _baseline() -> dict[str, Any]:
    now = int(datetime.now(UTC).timestamp())
    return {
        "sub": "00000000-0000-4000-8000-000000000001",
        "acc": "00000000-0000-4000-8000-000000000002",
        "kind": "user",
        "name": "Fuzz",
        "roles": ["owner"],
        "perms": ["mower.view"],
        "sid": "00000000-0000-4000-8000-000000000003",
        "jti": "00000000-0000-4000-8000-000000000004",
        "iss": DEFAULT_ISSUER,
        "aud": DEFAULT_AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
    }


def _verify(token: str) -> None:
    try:
        verified = run_sync(VERIFIER.verify_token(token))
    except TokenError:
        return
    claims = jwt.decode(token, options={"verify_signature": False})
    assert verified.principal.subject == UUID(claims["sub"])
    assert verified.expires_at == datetime.fromtimestamp(int(claims["exp"]), UTC)


def _decode_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _check_signed(inp: Input) -> None:
    header = _decode_json(inp.take(inp.int_in(0, 200)))
    override = _decode_json(inp.rest())
    claims = _baseline()
    if isinstance(override, dict):
        claims.update(override)
    headers = {"kid": KEY.kid, "typ": "at+jwt"}
    if isinstance(header, dict):
        headers.update(header)
    try:
        token = jwt.encode(claims, KEY.private_key, algorithm="EdDSA", headers=headers)
    except (TypeError, ValueError, NotImplementedError, jwt.PyJWTError):  # the harness signing
        return
    _verify(token)


def _check_api_token(text: str) -> None:
    parsed = credentials.parse_api_token(text)
    prefix = credentials.API_TOKEN_PREFIX
    if parsed is None:
        return
    token_id, secret = parsed
    # One spelling per token: exactly what new_api_token writes.
    assert text == f"{prefix}{token_id.hex}_{secret}"
    assert len(secret) >= 32


def _check_jwks(raw: bytes) -> None:
    document = _decode_json(raw)
    try:
        keys = parse_jwks(document)
    except ValueError:
        return
    assert all(isinstance(kid, str) for kid in keys)


def test_one_input(data: bytes) -> None:
    inp = Input(data)
    match inp.uint(1) % 4:
        case 0:
            _verify(inp.rest().decode("utf-8", "replace"))
        case 1:
            _check_signed(inp)
        case 2:
            _check_api_token(inp.rest().decode("utf-8", "replace"))
        case _:
            _check_jwks(inp.rest())


def seeds() -> list[bytes]:
    """A valid token, signed claims, an API token and a JWKS document."""
    token = jwt.encode(
        _baseline() | {"exp": int((datetime.now(UTC) + timedelta(days=3650)).timestamp())},
        KEY.private_key,
        algorithm="EdDSA",
        headers={"kid": KEY.kid, "typ": "at+jwt"},
    )
    header = json.dumps({"alg": "EdDSA"}).encode("utf-8")
    return [
        b"\x00" + token.encode("ascii"),
        b"\x01" + bytes([len(header)]) + header + json.dumps({"kind": "api_token"}).encode(),
        b"\x02" + credentials.new_api_token(UUID(int=7))[0].encode("ascii"),
        b"\x03" + json.dumps({"keys": [KEY.public_jwk(), {"kid": "x", "kty": "OKP"}]}).encode(),
    ]


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
