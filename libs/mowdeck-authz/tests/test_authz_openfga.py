"""OpenFgaAuthorizer against a scripted SDK client: resolution, fail closed, cache, batch."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from authz_fake_sdk import FakeFgaClient, connected_client, factory, store
from mowdeck_authz import (
    AuthzSettings,
    AuthzUnavailableError,
    CheckItem,
    FgaConnection,
    ModelNotFoundError,
    OpenFgaAuthorizer,
    StoreNotFoundError,
    account,
    mower,
    readiness_check_authz,
    user,
)
from mowdeck_authz.testing import ManualClock
from prometheus_client import CollectorRegistry

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
LUKAS = user("lukas")
EVE = user("eve")
M1 = mower("m1")


def settings(**overrides: Any) -> AuthzSettings:
    values: dict[str, Any] = {"api_url": "http://openfga:8080", "api_token": "key", **overrides}
    return AuthzSettings(**values)


def authorizer(
    client: FakeFgaClient,
    *,
    clock: ManualClock | None = None,
    registry: CollectorRegistry | None = None,
    **overrides: Any,
) -> OpenFgaAuthorizer:
    return OpenFgaAuthorizer(
        settings(**overrides),
        client_factory=factory(client),
        clock=clock or ManualClock(),
        now=lambda: NOW,
        registry=registry,
    )


def lukas_only(user_: str, relation: str, obj: str, ctx: dict[str, Any] | None) -> bool:
    return user_ == "user:lukas"


# --- Settings ---------------------------------------------------------------------------------


def test_settings_from_env(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("s3cret\n", encoding="utf-8")
    loaded = AuthzSettings.from_env(
        {
            "MOWDECK_OPENFGA_API_URL": "http://fga:8080",
            "MOWDECK_OPENFGA_API_TOKEN_FILE": str(secret),
            "MOWDECK_OPENFGA_STORE_ID": "01STORE",
            "MOWDECK_OPENFGA_CACHE_TTL_SECONDS": "0",
            "mowdeck_openfga_timeout_seconds": "1.5",
        }
    )
    assert loaded.api_token == "s3cret"  # noqa: S105 - test fixture
    assert loaded.store_id == "01STORE"
    assert loaded.store_name == "mowdeck"
    assert loaded.model_id is None
    assert loaded.cache_ttl_seconds == 0
    assert loaded.timeout_seconds == 1.5
    assert "s3cret" not in repr(loaded)


def test_settings_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="API_URL is required"):
        AuthzSettings.from_env({})
    with pytest.raises(ValueError, match="both"):
        AuthzSettings.from_env(
            {
                "MOWDECK_OPENFGA_API_URL": "http://x",
                "MOWDECK_OPENFGA_API_TOKEN": "a",
                "MOWDECK_OPENFGA_API_TOKEN_FILE": str(tmp_path / "t"),
            }
        )
    with pytest.raises(ValueError, match="api_url"):
        AuthzSettings(api_url="")
    with pytest.raises(ValueError, match="timeout"):
        AuthzSettings(api_url="http://x", timeout_seconds=0)


def test_from_env_builds_an_authorizer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOWDECK_OPENFGA_API_URL", "http://fga:8080")
    built = OpenFgaAuthorizer.from_env()
    assert built.connection.settings.api_url == "http://fga:8080"
    assert built.store_id is None


# --- Store and model resolution ---------------------------------------------------------------


async def test_connect_resolves_store_by_name_and_pins_latest_model() -> None:
    client = connected_client("01M")
    client.stores = [
        store("s2", created_at="t5"),
        store("other", "x"),
        store("s1", created_at="t1"),
    ]
    client.models["s1"].append(client.models["s1"][0])
    authz = authorizer(client)
    await authz.connect()
    await authz.connect()  # idempotent
    assert (authz.store_id, authz.model_id) == ("s1", "01M")  # oldest store of that name wins
    assert (client.store_id, client.model_id) == ("s1", "01M")
    assert client.names().count("list_stores") == 1


async def test_store_listing_follows_pages() -> None:
    client = connected_client()
    client.stores = [store(f"x{i}", "other") for i in range(3)] + [store("s1")]
    client.models["s1"] = connected_client().models["s1"]
    client.page_size_stores = 2
    authz = authorizer(client)
    await authz.connect()
    assert authz.store_id == "s1"
    assert client.names().count("list_stores") == 2


async def test_pinned_ids_skip_the_lookups() -> None:
    client = FakeFgaClient()
    authz = authorizer(client, store_id="01S", model_id="01M")
    await authz.connect()
    assert (client.store_id, client.model_id) == ("01S", "01M")
    assert "list_stores" not in client.names()
    assert "read_latest_authorization_model" not in client.names()


async def test_missing_store_or_model_is_reported() -> None:
    client = FakeFgaClient()
    with pytest.raises(StoreNotFoundError, match="authz-bootstrap"):
        await authorizer(client).connect()
    assert client.closed
    client.stores = [store("s1")]
    with pytest.raises(ModelNotFoundError):
        await authorizer(client).connect()


async def test_connection_errors_become_unavailable() -> None:
    client = connected_client()
    client.fail["list_stores"] = OSError("connection refused")
    connection = FgaConnection(settings(), client_factory=factory(client))
    with pytest.raises(AuthzUnavailableError, match="OSError"):
        await connection.connect()
    with pytest.raises(AuthzUnavailableError):
        _ = connection.client
    assert not connection.is_connected


# --- Checks and fail closed -------------------------------------------------------------------


async def test_check_sends_current_time_and_pinned_model() -> None:
    client = connected_client()
    client.allowed = lukas_only
    authz = authorizer(client)
    assert await authz.check(LUKAS, "can_view", M1)
    assert not await authz.check(EVE, "can_view", M1, context={"extra": 1})
    checks = [body for name, body in client.calls if name == "check"]
    assert checks[0].context == {"current_time": "2026-10-09T12:00:00Z"}
    assert checks[1].context == {"current_time": "2026-10-09T12:00:00Z", "extra": 1}
    assert checks[0].object == "mower:m1"
    assert client.model_id == "01MODEL"


async def test_sdk_errors_deny_and_are_counted(caplog: pytest.LogCaptureFixture) -> None:
    client = connected_client()
    client.allowed = lukas_only
    registry = CollectorRegistry()
    authz = authorizer(client, registry=registry)
    client.fail["check"] = TimeoutError()
    with caplog.at_level(logging.WARNING, logger="mowdeck_authz"):
        assert await authz.check(LUKAS, "can_view", M1) is False
    assert "fail closed" in caplog.text
    assert registry.get_sample_value("mowdeck_authz_errors_total", {"operation": "check"}) == 1
    # Errors are not cached: once OpenFGA answers again, the real answer comes through.
    del client.fail["check"]
    assert await authz.check(LUKAS, "can_view", M1) is True
    assert registry.get_sample_value("mowdeck_authz_decisions_total", {"outcome": "denied"}) == 1
    assert registry.get_sample_value("mowdeck_authz_decisions_total", {"outcome": "allowed"}) == 1


async def test_unreachable_openfga_denies_then_recovers_lazily() -> None:
    client = connected_client()
    client.allowed = lukas_only
    client.fail["list_stores"] = OSError("down")
    registry = CollectorRegistry()
    authz = authorizer(client, registry=registry)
    assert await authz.check(LUKAS, "can_view", M1) is False
    assert await authz.check_many([(LUKAS, "can_view", M1), (EVE, "can_view", M1)]) == [
        False,
        False,
    ]
    assert await authz.list_objects(LUKAS, "can_view", "mower") == []
    assert registry.get_sample_value("mowdeck_authz_errors_total", {"operation": "connect"}) == 4
    del client.fail["list_stores"]
    assert await authz.check(LUKAS, "can_view", M1) is True


async def test_batch_check_maps_results_by_correlation_id() -> None:
    client = connected_client()
    client.allowed = lambda u, r, o, c: r == "can_view" or u == "user:lukas"
    authz = authorizer(client, cache_ttl_seconds=0)
    items = [
        CheckItem(EVE, "can_control", M1),
        CheckItem(EVE, "can_view", M1),
        CheckItem(LUKAS, "can_control", M1),
        CheckItem(EVE, "can_drive", M1),
    ]
    assert await authz.check_many(items) == [False, True, True, False]
    batch = next(body for name, body in client.calls if name == "batch_check")
    assert [c.correlation_id for c in batch.checks] == ["0", "1", "2", "3"]
    assert all(c.context == {"current_time": "2026-10-09T12:00:00Z"} for c in batch.checks)


async def test_batch_item_errors_and_missing_results_deny() -> None:
    client = connected_client()
    client.allowed = lambda u, r, o, c: True
    client.batch_errors = {"0"}
    client.batch_drop = {"2"}
    registry = CollectorRegistry()
    authz = authorizer(client, registry=registry)
    items = [(LUKAS, "can_view", mower(f"m{i}")) for i in range(4)]
    assert await authz.check_many(items) == [False, True, False, True]
    assert (
        registry.get_sample_value("mowdeck_authz_errors_total", {"operation": "batch_check_item"})
        == 1
    )
    client.fail["batch_check"] = RuntimeError("500")
    assert await authz.check_many([(EVE, "can_view", M1), (EVE, "can_drive", M1)]) == [
        False,
        False,
    ]


async def test_empty_batch() -> None:
    client = connected_client()
    assert await authorizer(client).check_many([]) == []
    assert "batch_check" not in client.names()


# --- Cache ------------------------------------------------------------------------------------


async def test_cache_ttl_and_invalidation() -> None:
    client = connected_client()
    client.allowed = lukas_only
    clock = ManualClock()
    registry = CollectorRegistry()
    authz = authorizer(client, clock=clock, registry=registry)

    def asked() -> int:
        return client.names().count("check")

    assert await authz.check(LUKAS, "can_view", M1)
    assert await authz.check(LUKAS, "can_view", M1)
    assert asked() == 1
    assert registry.get_sample_value("mowdeck_authz_cache_hits_total") == 1
    clock.advance(4.9)
    assert await authz.check(LUKAS, "can_view", M1)
    assert asked() == 1
    clock.advance(0.2)  # past the 5 s TTL
    assert await authz.check(LUKAS, "can_view", M1)
    assert asked() == 2

    # Negative answers are cached too; contexts are part of the key.
    assert not await authz.check(EVE, "can_view", M1)
    assert not await authz.check(EVE, "can_view", M1)
    assert not await authz.check(EVE, "can_view", M1, context={"x": 1})
    assert asked() == 4

    authz.invalidate_subject(EVE)
    assert not await authz.check(EVE, "can_view", M1)
    assert await authz.check(LUKAS, "can_view", M1)
    assert asked() == 5
    authz.invalidate()
    assert await authz.check(LUKAS, "can_view", M1)
    assert asked() == 6


async def test_cache_hits_are_skipped_in_batches() -> None:
    client = connected_client()
    client.allowed = lukas_only
    authz = authorizer(client)
    assert await authz.check(LUKAS, "can_view", M1)
    result = await authz.check_many(
        [(LUKAS, "can_view", M1), (LUKAS, "can_drive", M1), (EVE, "can_view", M1)]
    )
    assert result == [True, True, False]
    batch = next(body for name, body in client.calls if name == "batch_check")
    assert [(c.user, c.relation) for c in batch.checks] == [
        ("user:lukas", "can_drive"),
        ("user:eve", "can_view"),
    ]
    # One left over after cache hits goes through a single check instead of a batch.
    assert await authz.check_many([(LUKAS, "can_view", M1), (LUKAS, "can_control", M1)]) == [
        True,
        True,
    ]
    assert client.names()[-1] == "check"


async def test_sensitive_relations_and_ttl_zero_bypass_the_cache() -> None:
    client = connected_client()
    client.allowed = lukas_only
    authz = authorizer(client)
    for _ in range(2):
        assert await authz.check(LUKAS, "can_manage_users", account("A"))
    assert client.names().count("check") == 2
    uncached = authorizer(connected_client(), cache_ttl_seconds=0)
    await uncached.check(LUKAS, "can_view", M1)
    assert len(uncached.cache) == 0
    assert not uncached.cache.enabled


async def test_cache_is_bounded() -> None:
    client = connected_client()
    authz = OpenFgaAuthorizer(
        AuthzSettings(api_url="http://x", cache_max_entries=2), client_factory=factory(client)
    )
    for i in range(3):
        await authz.check(LUKAS, "can_view", mower(f"m{i}"))
    assert len(authz.cache) == 2


# --- list_objects, readiness, close -----------------------------------------------------------


async def test_list_objects() -> None:
    client = connected_client()
    client.objects = ["mower:m1", "mower:m2"]
    authz = authorizer(client)
    assert await authz.list_objects(LUKAS, "can_view", "mower") == ["mower:m1", "mower:m2"]
    body = next(b for n, b in client.calls if n == "list_objects")
    assert (body.user, body.relation, body.type) == ("user:lukas", "can_view", "mower")
    assert body.context == {"current_time": "2026-10-09T12:00:00Z"}
    client.fail["list_objects"] = RuntimeError()
    assert await authz.list_objects(LUKAS, "can_view", "mower") == []


async def test_readiness_check() -> None:
    client = connected_client()
    authz = authorizer(client)
    check = readiness_check_authz(authz)
    assert check.__name__ == "authz"
    assert await check() is True
    client.fail["get_store"] = OSError()
    assert await check() is False
    down = FakeFgaClient()
    assert await readiness_check_authz(authorizer(down))() is False


async def test_close_and_reconnect() -> None:
    client = connected_client()
    authz = authorizer(client)
    await authz.connect()
    await authz.close()
    assert client.closed
    assert not authz.connection.is_connected
    await authz.close()  # closing twice is fine
    assert await authz.ping()


async def test_shared_connection() -> None:
    client = connected_client()
    connection = FgaConnection(settings(), client_factory=factory(client))
    authz = OpenFgaAuthorizer(connection)
    assert authz.connection is connection
    await connection.connect()
    assert authz.store_id == "s1"
