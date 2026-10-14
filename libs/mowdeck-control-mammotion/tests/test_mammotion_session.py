"""Shared cloud sessions, device discovery and the fake cloud (ADR 0034)."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from mammotion_support import ACCOUNT_ID
from mowdeck_control import ConnectionConfig
from mowdeck_control_mammotion import cloud as cloud_module
from mowdeck_control_mammotion.cloud import (
    cloud_connection_factory,
    default_pool,
    open_pooled_cloud_client,
)
from mowdeck_control_mammotion.discovery import (
    CloudLoginRefused,
    CloudUnavailable,
    device_of,
    discover_devices,
    registry_devices,
)
from mowdeck_control_mammotion.fake import sample_device
from mowdeck_control_mammotion.fake_cloud import (
    FAKE_CLOUD_PASSWORD,
    FakeCloudClient,
    fake_cloud_connection_factory,
    fake_devices,
    fake_pool,
    is_fake_account,
)
from mowdeck_control_mammotion.options import CloudOptions
from mowdeck_control_mammotion.port import AuthError, ClientPort, LoginFailedError, OpenedClient
from mowdeck_control_mammotion.session import (
    CloudSessionPool,
    PoolClosedError,
    address_tag,
    session_key,
)

ACCOUNT = "garden@example.com"
PASSWORD = "secret-password"  # noqa: S105 - a test value


class FakeClient:
    """Enough of ``MammotionClient`` for the pool, the pooled opener and the discovery."""

    def __init__(self, *names: str) -> None:
        self.reauth_required: str | None = None
        self.stopped = 0
        self.refreshed: list[str] = []
        handles = [
            SimpleNamespace(
                device_name=name,
                iot_id=f"iot-{name}",
                product_key="",
                snapshot=SimpleNamespace(
                    raw=object() if name.startswith("RTK") else sample_device(name)
                ),
            )
            for name in names
        ]
        self.device_registry = SimpleNamespace(all_devices=handles)

    async def stop(self) -> None:
        self.stopped += 1

    async def refresh_status(self, device_name: str) -> None:
        self.refreshed.append(device_name)


class Logins:
    """A login function that counts and can fail."""

    def __init__(self, *names: str) -> None:
        self.names = names or ("Luba-VPA", "Luba-VPB", "RTK-1")
        self.calls: list[tuple[str, Path | None]] = []
        self.clients: list[FakeClient] = []
        self.error: Exception | None = None
        self.delay = 0.0

    async def __call__(self, account: str, password: str, cache: Path | None) -> FakeClient:
        self.calls.append((account, cache))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        client = FakeClient(*self.names)
        self.clients.append(client)
        return client


def pool(logins: Logins, linger: float = 0.0) -> CloudSessionPool:
    return CloudSessionPool(logins, linger_seconds=linger)


# --- the pool -------------------------------------------------------------------------------


def test_session_key_folds_the_address_and_hides_both_values() -> None:
    key = session_key(" Garden@Example.com ", PASSWORD)
    assert key == session_key("garden@example.com", PASSWORD)
    assert "garden" not in "".join(key) and PASSWORD not in "".join(key)
    assert session_key("garden@example.com", "other") != key
    assert session_key("garden@example.com", "other")[0] == key[0]  # same address
    assert address_tag(" Garden@Example.com") == address_tag("garden@example.com")
    assert "garden" not in address_tag(ACCOUNT) and len(address_tag(ACCOUNT)) == 24


async def test_the_credential_cache_is_per_address(tmp_path: Path) -> None:
    logins = Logins()
    sessions = CloudSessionPool(logins, linger_seconds=0, cache_dir=tmp_path)
    lease = await sessions.acquire(ACCOUNT, PASSWORD)
    await lease.release()
    assert logins.calls == [(ACCOUNT, tmp_path / f"{address_tag(ACCOUNT)}.json")]
    assert pool(Logins()).cache_path(ACCOUNT) is None


async def test_a_new_password_retires_the_old_session() -> None:
    logins = Logins()
    sessions = pool(logins, linger=60)
    idle = await sessions.acquire(ACCOUNT, PASSWORD)
    await idle.release()  # lingering
    held = await sessions.acquire("other@example.com", PASSWORD)
    fresh = await sessions.acquire(ACCOUNT, "new-password")
    assert logins.clients[0].stopped == 1  # the unused old session went at once
    assert logins.clients[1].stopped == 0  # another address is not touched
    in_use = await sessions.acquire(ACCOUNT, "newer-password")
    await fresh.release()  # superseded: no linger
    assert logins.clients[2].stopped == 1
    await in_use.release()
    await held.release()
    await sessions.close()


async def test_a_closed_pool_refuses_and_stops_a_late_login() -> None:
    logins = Logins()
    logins.delay = 0.05
    sessions = pool(logins)
    pending = asyncio.create_task(sessions.acquire(ACCOUNT, PASSWORD))
    await asyncio.sleep(0.01)
    await sessions.close()
    with pytest.raises(PoolClosedError):
        await pending
    assert logins.clients[0].stopped == 1
    with pytest.raises(PoolClosedError):
        await sessions.acquire(ACCOUNT, PASSWORD)


async def test_locks_are_dropped_with_their_sessions() -> None:
    sessions = pool(Logins())
    lease = await sessions.acquire(ACCOUNT, PASSWORD)
    await lease.release()
    assert sessions.open_sessions == 0
    assert sessions._locks == {}


async def test_two_leases_share_one_login_and_the_last_release_stops_it() -> None:
    logins = Logins()
    sessions = pool(logins)
    first = await sessions.acquire(ACCOUNT, PASSWORD)
    second = await sessions.acquire(ACCOUNT.upper(), PASSWORD)
    assert len(logins.calls) == 1 and sessions.logins == 1
    assert first.client is second.client
    assert (first.reused, second.reused) == (False, True)
    assert sessions.users(ACCOUNT, PASSWORD) == 2 and sessions.is_open(ACCOUNT, PASSWORD)
    await first.release()
    await first.release()  # idempotent
    assert first.released and logins.clients[0].stopped == 0
    await second.release()
    assert logins.clients[0].stopped == 1
    assert sessions.open_sessions == 0 and not sessions.is_open(ACCOUNT, PASSWORD)


async def test_other_credentials_get_their_own_session() -> None:
    logins = Logins()
    sessions = pool(logins)
    one = await sessions.acquire(ACCOUNT, PASSWORD)
    other = await sessions.acquire(ACCOUNT, "changed-password")
    assert one.client is not other.client and sessions.logins == 2
    await one.release()
    await other.release()


async def test_concurrent_acquires_wait_for_one_login() -> None:
    logins = Logins()
    logins.delay = 0.01
    sessions = pool(logins)
    leases = await asyncio.gather(*(sessions.acquire(ACCOUNT, PASSWORD) for _ in range(3)))
    assert len(logins.calls) == 1
    assert sorted(lease.reused for lease in leases) == [False, True, True]
    for lease in leases:
        await lease.release()


async def test_an_unused_session_lingers_and_is_reused() -> None:
    logins = Logins()
    sessions = pool(logins, linger=0.05)
    lease = await sessions.acquire(ACCOUNT, PASSWORD)
    await lease.release()
    assert logins.clients[0].stopped == 0 and sessions.open_sessions == 1
    again = await sessions.acquire(ACCOUNT, PASSWORD)
    assert again.reused and sessions.logins == 1
    await again.release()
    await asyncio.sleep(0.1)
    assert logins.clients[0].stopped == 1 and sessions.open_sessions == 0


async def test_a_dead_session_is_replaced_and_stopped_once_released() -> None:
    logins = Logins()
    sessions = pool(logins)
    old = await sessions.acquire(ACCOUNT, PASSWORD)
    logins.clients[0].reauth_required = "refresh token rejected"
    assert not sessions.is_open(ACCOUNT, PASSWORD)
    fresh = await sessions.acquire(ACCOUNT, PASSWORD)
    assert not fresh.reused and fresh.client is logins.clients[1]
    await old.release()
    assert logins.clients[0].stopped == 1 and logins.clients[1].stopped == 0
    await fresh.release()
    assert logins.clients[1].stopped == 1


async def test_a_failed_login_leaves_no_session() -> None:
    logins = Logins()
    logins.error = LoginFailedError(ACCOUNT, "wrong password")
    sessions = pool(logins)
    with pytest.raises(LoginFailedError):
        await sessions.acquire(ACCOUNT, PASSWORD)
    assert sessions.open_sessions == 0
    logins.error = None
    lease = await sessions.acquire(ACCOUNT, PASSWORD)
    assert not lease.reused
    await lease.release()


async def test_close_stops_every_session() -> None:
    logins = Logins()
    sessions = pool(logins, linger=60)
    held = await sessions.acquire(ACCOUNT, PASSWORD)
    idle = await sessions.acquire("other@example.com", PASSWORD)
    await idle.release()
    await sessions.close()
    assert [c.stopped for c in logins.clients] == [1, 1]
    await held.release()  # after close: nothing left to stop twice
    assert logins.clients[0].stopped == 1


async def test_a_failing_stop_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    logins = Logins()
    sessions = pool(logins)
    lease = await sessions.acquire(ACCOUNT, PASSWORD)

    async def broken() -> None:
        raise RuntimeError("boom")

    logins.clients[0].stop = broken  # type: ignore[method-assign]
    await lease.release()
    assert "stopping a pooled Mammotion client failed" in caplog.text


# --- shared clients behind ClientPort ------------------------------------------------------


def _port(sessions: CloudSessionPool, device_name: str) -> ClientPort:
    async def opener() -> OpenedClient:
        return await open_pooled_cloud_client(
            sessions,
            account=ACCOUNT,
            password=PASSWORD,
            device_name=device_name,
            iot_id=None,
            settle_seconds=0,
        )

    return ClientPort(opener)


async def test_two_mowers_of_one_account_share_one_login() -> None:
    logins = Logins()
    sessions = pool(logins)
    front, back = _port(sessions, "Luba-VPA"), _port(sessions, "Luba-VPB")
    await front.open()
    await back.open()
    assert len(logins.calls) == 1
    client = logins.clients[0]
    assert front.device_name == "Luba-VPA" and back.device_name == "Luba-VPB"
    assert client.refreshed == ["Luba-VPA", "Luba-VPB"]
    assert front.is_open and back.is_open
    await front.close()
    assert client.stopped == 0 and back.is_open and not front.is_open
    await back.close()
    assert client.stopped == 1


async def test_an_unknown_or_non_mower_device_releases_its_lease() -> None:
    logins = Logins()
    sessions = pool(logins)
    with pytest.raises(LookupError, match="not on this account"):
        await _port(sessions, "Luba-NOPE").open()
    with pytest.raises(LookupError, match="not a mower"):
        await _port(sessions, "RTK-1").open()
    assert sessions.open_sessions == 0
    assert [c.stopped for c in logins.clients] == [1, 1]


async def test_cloud_factory_builds_connections_on_the_given_pool() -> None:
    logins = Logins()
    sessions = pool(logins)
    create = cloud_connection_factory(sessions)
    options = {"account": ACCOUNT, "password": PASSWORD, "device_name": "Luba-VPA"}
    connection = create(ConnectionConfig(mower_id=uuid4(), account_id=ACCOUNT_ID, options=options))
    assert connection.options.device_name == "Luba-VPA"


def test_default_pool_is_one_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_module, "_DEFAULT_POOL", None)
    assert default_pool() is default_pool()


def test_linked_mowers_pass_the_option_check_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    options = {
        "account": ACCOUNT,
        "password": PASSWORD,
        "device_name": "Luba-VPA",
        "linked_account_id": str(uuid4()),
    }
    assert CloudOptions.parse(options).password == PASSWORD
    assert "plain 'password'" not in caplog.text


# --- discovery ------------------------------------------------------------------------------


def test_registry_devices_mark_base_stations_as_not_mowers() -> None:
    devices = registry_devices(FakeClient("Luba-VPA", "RTK-1"))
    assert [(d.device_name, d.is_mower) for d in devices] == [("Luba-VPA", True), ("RTK-1", False)]
    mower = devices[0]
    assert mower.iot_id == "iot-Luba-VPA" and mower.model and mower.online is True


def test_device_of_survives_a_handle_without_snapshot() -> None:
    found = device_of(SimpleNamespace(device_name="Luba-X", iot_id="", product_key=None))
    assert found.is_mower is False and found.iot_id is None and found.online is None


async def test_discovery_lists_mowers_first_and_reuses_an_open_session() -> None:
    logins = Logins("RTK-1", "Luba-VPB", "Luba-VPA")
    sessions = pool(logins, linger=60)
    first = await discover_devices(sessions, ACCOUNT, PASSWORD)
    assert [d.device_name for d in first.devices] == ["Luba-VPA", "Luba-VPB", "RTK-1"]
    assert not first.reused_session
    second = await discover_devices(sessions, ACCOUNT, PASSWORD)
    assert second.reused_session and len(logins.calls) == 1
    await sessions.close()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LoginFailedError(ACCOUNT, "bad"), CloudLoginRefused),
        (AuthError("expired"), CloudLoginRefused),
        (TimeoutError(), CloudUnavailable),
        (OSError("dns"), CloudUnavailable),
    ],
)
async def test_discovery_maps_login_errors(error: Exception, expected: type[Exception]) -> None:
    logins = Logins()
    logins.error = error
    with pytest.raises(expected) as raised:
        await discover_devices(pool(logins), ACCOUNT, PASSWORD)
    assert PASSWORD not in str(raised.value) and ACCOUNT not in str(raised.value)


async def test_discovery_reports_a_broken_device_list() -> None:
    def broken(client: Any) -> list[Any]:
        raise KeyError("records")

    sessions = pool(Logins())
    with pytest.raises(CloudUnavailable, match="KeyError"):
        await discover_devices(sessions, ACCOUNT, PASSWORD, lister=broken)
    assert sessions.open_sessions == 0


# --- the fake cloud -------------------------------------------------------------------------

FAKE_ACCOUNT = "garden@fake.mowdeck.test"


async def test_fake_cloud_lists_its_devices_for_the_right_password() -> None:
    sessions = fake_pool(linger_seconds=0)
    found = await discover_devices(sessions, FAKE_ACCOUNT, FAKE_CLOUD_PASSWORD, lister=fake_devices)
    mowers = [d.device_name for d in found.devices if d.is_mower]
    assert mowers == ["Luba-FAKE0001", "Luba-FAKE0002"]
    assert [d.online for d in found.devices] == [True, False, True]
    with pytest.raises(CloudLoginRefused):
        await discover_devices(sessions, FAKE_ACCOUNT, "wrong", lister=fake_devices)
    with pytest.raises(CloudLoginRefused):
        await discover_devices(sessions, ACCOUNT, FAKE_CLOUD_PASSWORD, lister=fake_devices)
    assert is_fake_account(" Garden@FAKE.mowdeck.test") and not is_fake_account(ACCOUNT)
    with pytest.raises(TypeError):
        fake_devices(object())


async def test_fake_cloud_mowers_share_one_session() -> None:
    sessions = fake_pool(linger_seconds=0)
    create = fake_cloud_connection_factory(sessions)

    def config(device_name: str) -> ConnectionConfig:
        options = {
            "account": FAKE_ACCOUNT,
            "password": FAKE_CLOUD_PASSWORD,
            "device_name": device_name,
            "linked_account_id": str(uuid4()),
        }
        return ConnectionConfig(mower_id=uuid4(), account_id=ACCOUNT_ID, options=options)

    first, second = create(config("Luba-FAKE0001")), create(config("Luba-FAKE0002"))
    await first.connect()
    await second.connect()
    assert sessions.logins == 1 and first.is_connected and second.is_connected
    assert (await first.get_state()).battery_percent is not None
    await first.disconnect()
    assert sessions.open_sessions == 1
    await second.disconnect()
    assert sessions.open_sessions == 0
    with pytest.raises(LookupError, match="not a mower"):
        await create(config("RTK-FAKE0001")).connect()
    assert sessions.open_sessions == 0
    client = FakeCloudClient(FAKE_ACCOUNT)
    await client.stop()
    assert client.stopped
