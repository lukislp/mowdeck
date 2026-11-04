# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""JWKS cache: lifespan, refresh on unknown kid, cooldown, stale keys, parsing; keys."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from mowdeck_auth import (
    AuthSettings,
    JwksCache,
    SigningKey,
    StaticKeySource,
    parse_jwks,
    public_jwk,
    thumbprint,
)
from mowdeck_auth.keys import b64url, b64url_decode
from mowdeck_auth.testing import TEST_JWKS_URL, TestKeys
from mowdeck_contracts.v1 import Principal, PrincipalKind


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def principal() -> Principal:
    return Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER)


async def test_keys_are_cached_for_the_lifespan() -> None:
    keys, clock = TestKeys(), Clock()
    cache = keys.cache(lifespan=300, refresh_cooldown=30, clock=clock)
    assert await cache.key_for(keys.key.kid) is not None
    assert await cache.key_for(keys.key.kid) is not None
    assert keys.server.requests == 1
    clock.now += 301
    assert await cache.key_for(keys.key.kid) is not None
    assert keys.server.requests == 2


async def test_unknown_kid_triggers_one_refresh_so_rotation_verifies_at_once() -> None:
    keys, clock = TestKeys(), Clock()
    verifier = keys.verifier(lifespan=300, refresh_cooldown=30, clock=clock)
    assert (await verifier.verify(keys.mint(principal()))).kind is PrincipalKind.USER
    clock.now += 31
    keys.rotate()
    assert (await verifier.verify(keys.mint(principal()))).kind is PrincipalKind.USER
    assert keys.server.requests == 2


async def test_refreshes_are_rate_limited_by_the_cooldown() -> None:
    keys, clock = TestKeys(), Clock()
    cache = keys.cache(lifespan=300, refresh_cooldown=30, clock=clock)
    await cache.key_for(keys.key.kid)
    for _ in range(20):
        assert await cache.key_for(f"random-{uuid4()}") is None
    assert keys.server.requests == 1  # no fetch storm for random kids
    clock.now += 30
    assert await cache.key_for("still-unknown") is None
    assert keys.server.requests == 2


async def test_stale_keys_are_served_while_identity_is_down_then_dropped() -> None:
    keys, clock = TestKeys(), Clock()
    cache = keys.cache(lifespan=300, refresh_cooldown=30, stale_max=3600, clock=clock)
    assert await cache.key_for(keys.key.kid) is not None
    keys.server.fail = True
    clock.now += 301
    assert await cache.key_for(keys.key.kid) is not None  # stale but trusted
    clock.now += 3600
    assert await cache.key_for(keys.key.kid) is None  # too old: dropped
    keys.server.fail = False
    clock.now += 30
    assert await cache.key_for(keys.key.kid) is not None


async def test_first_fetch_failure_rejects_and_retries_after_cooldown() -> None:
    keys, clock = TestKeys(), Clock()
    keys.server.fail = True
    cache = keys.cache(refresh_cooldown=30, clock=clock)
    assert await cache.key_for(keys.key.kid) is None
    assert not await cache.refresh()
    keys.server.fail = False
    clock.now += 31
    assert await cache.key_for(keys.key.kid) is not None


class SlowJwks(httpx.AsyncBaseTransport):
    """Answers the JWKS only once ``release`` is set, so concurrent lookups overlap a fetch."""

    def __init__(self, keys: TestKeys) -> None:
        self.keys = keys
        self.release = asyncio.Event()
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        await self.release.wait()
        return httpx.Response(200, json=self.keys.server.document())


async def test_a_burst_on_a_fresh_cache_waits_for_the_fetch_in_flight() -> None:
    keys = TestKeys()
    transport = SlowJwks(keys)
    cache = JwksCache(TEST_JWKS_URL, transport=transport, refresh_cooldown=30)
    lookups = [asyncio.create_task(cache.key_for(keys.key.kid)) for _ in range(20)]
    await asyncio.sleep(0.01)  # every lookup has started; the first one fetches
    assert not any(task.done() for task in lookups)  # none gave up as "unknown key"
    transport.release.set()
    found = await asyncio.gather(*lookups)
    assert all(key is not None for key in found)
    assert transport.requests == 1


async def test_an_unknown_kid_during_a_fetch_waits_once_and_fetches_nothing_more() -> None:
    keys = TestKeys()
    transport = SlowJwks(keys)
    cache = JwksCache(TEST_JWKS_URL, transport=transport, refresh_cooldown=30)
    first = asyncio.create_task(cache.key_for(keys.key.kid))
    await asyncio.sleep(0.01)
    random_kids = [asyncio.create_task(cache.key_for(f"random-{uuid4()}")) for _ in range(5)]
    transport.release.set()
    assert await first is not None
    assert await asyncio.gather(*random_kids) == [None] * 5
    assert transport.requests == 1


async def test_unreachable_url_is_handled() -> None:
    cache = JwksCache("http://127.0.0.1:9/jwks.json", timeout=0.2)
    assert await cache.key_for("x") is None


def test_parse_jwks_skips_unusable_entries() -> None:
    good = SigningKey.generate()
    document = {
        "keys": [
            good.public_jwk(),
            {"kty": "OKP", "crv": "Ed25519", "x": "!!", "kid": "broken"},
            {**SigningKey.generate().public_jwk(), "use": "enc"},
            {"kty": "OKP", "crv": "Ed25519", "x": good.public_jwk()["x"]},  # no kid
            "not a dict",
        ]
    }
    assert set(parse_jwks(document)) == {good.kid}
    with pytest.raises(ValueError, match="keys"):
        parse_jwks({"nokeys": []})


async def test_static_key_source_can_be_replaced() -> None:
    first, second = SigningKey.generate(), SigningKey.generate()
    source = StaticKeySource({"keys": [first.public_jwk()]})
    assert await source.key_for(first.kid) is not None
    source.replace([second.public_jwk()])
    assert await source.key_for(first.kid) is None
    assert await source.key_for(second.kid) is not None


def test_key_round_trip_and_thumbprint() -> None:
    key = SigningKey.generate()
    again = SigningKey.from_raw(key.raw())
    assert again.kid == key.kid == thumbprint(key.public_key)
    jwk = public_jwk(key.public_key)
    assert jwk == {**key.public_jwk()}
    assert jwk["kty"] == "OKP" and jwk["crv"] == "Ed25519" and jwk["alg"] == "EdDSA"
    assert b64url_decode(b64url(b"\x00\x01binary")) == b"\x00\x01binary"
    assert "private" not in repr(key).lower() or "private_key" not in repr(key)


def test_rfc8037_thumbprint_example() -> None:
    # RFC 8037, appendix A.3: thumbprint of the example Ed25519 public key.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    x = b64url_decode("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo")
    assert thumbprint(Ed25519PublicKey.from_public_bytes(x)) == (
        "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"
    )


def test_settings_from_env() -> None:
    assert AuthSettings.from_env({}) is None
    settings = AuthSettings.from_env(
        {
            "MOWDECK_AUTH_JWKS_URL": TEST_JWKS_URL,
            "MOWDECK_AUTH_ISSUER": "urn:x",
            "mowdeck_auth_leeway_seconds": "5",
        }
    )
    assert settings is not None
    assert (settings.issuer, settings.audience, settings.leeway_seconds) == (
        "urn:x",
        "mowdeck-api",
        5,
    )
    with pytest.raises(ValueError, match="http"):
        AuthSettings(jwks_url="ftp://x")
    with pytest.raises(ValueError, match="negative"):
        AuthSettings(jwks_url=TEST_JWKS_URL, leeway_seconds=-1)


@pytest.mark.parametrize("document", [None, [], "keys", 3])
def test_parse_jwks_rejects_a_document_that_is_not_an_object(document: object) -> None:
    # Found by fuzzing (fuzz/fuzz_access_token.py): JwksCache.refresh passes response.json()
    # and handles ValueError; a JSON array or null raised AttributeError instead.
    from mowdeck_auth.jwks import parse_jwks

    with pytest.raises(ValueError, match="not an object"):
        parse_jwks(document)


def test_parse_jwks_skips_a_key_without_its_required_members() -> None:
    # Found by fuzzing: PyJWK.from_dict raised KeyError for an "oct" key without "k".
    from mowdeck_auth.jwks import parse_jwks

    assert parse_jwks({"keys": [{"kid": "a", "kty": "oct"}, {"kid": "b", "kty": "OKP"}]}) == {}
