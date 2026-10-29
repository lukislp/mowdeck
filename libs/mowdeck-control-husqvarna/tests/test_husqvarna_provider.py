"""The Husqvarna provider plugin on the fake cloud, end to end, and secret hygiene."""

import logging
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import CommandStatus, MowerActivity, ResumeMow
from mowdeck_control import (
    CLOUD_PROVIDER_OPTION,
    CloudLoginRefused,
    ConnectionConfig,
    ProviderContext,
)
from mowdeck_control_husqvarna.connection import HusqvarnaConnection
from mowdeck_control_husqvarna.fake_cloud import (
    FAKE_APP_KEY,
    FAKE_APP_SECRET,
    FAKE_MOWER_OFFLINE,
    FAKE_MOWER_ONLINE,
)
from mowdeck_control_husqvarna.provider import HusqvarnaProvider, create_provider

CREDENTIALS = {"app_key": FAKE_APP_KEY, "app_secret": FAKE_APP_SECRET}


def _config(device_name: str, iot_id: str) -> ConnectionConfig:
    return ConnectionConfig(
        mower_id=uuid4(),
        account_id=uuid4(),
        options={
            CLOUD_PROVIDER_OPTION: "husqvarna",
            **CREDENTIALS,
            "device_name": device_name,
            "iot_id": iot_id,
            "linked_account_id": str(uuid4()),
        },
    )


async def test_fake_provider_discovers_and_reuses_the_session() -> None:
    plugin = create_provider(ProviderContext(fake=True))
    try:
        found = await plugin.discover(CREDENTIALS)
        assert [(d.device_name, d.iot_id, d.online) for d in found.devices] == [
            ("Greta", FAKE_MOWER_OFFLINE, False),
            ("Hugo", FAKE_MOWER_ONLINE, True),
        ]
        assert [d.model for d in found.devices] == ["Automower 305", "Automower 430X NERA"]
        assert not found.reused_session
        again = await plugin.discover(CREDENTIALS)
        assert again.reused_session and plugin.pool.logins == 1
        other = await plugin.discover(
            {"app_key": "fake-husqvarna-second", "app_secret": FAKE_APP_SECRET}
        )
        assert len(other.devices) == 2 and plugin.pool.logins == 2
    finally:
        await plugin.close()
    assert plugin.pool.open_sessions == 0


@pytest.mark.parametrize(
    "credentials",
    [
        {"app_key": FAKE_APP_KEY, "app_secret": "wrong-secret"},
        {"app_key": "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d", "app_secret": FAKE_APP_SECRET},
        {"app_key": FAKE_APP_KEY},
        {"app_key": " ", "app_secret": FAKE_APP_SECRET},
    ],
)
async def test_fake_provider_refuses_wrong_credentials(credentials: dict[str, str]) -> None:
    plugin = create_provider(ProviderContext(fake=True))
    try:
        with pytest.raises(CloudLoginRefused) as caught:
            await plugin.discover(credentials)
        for value in credentials.values():
            if value.strip():
                assert value not in str(caught.value)
        assert plugin.pool.open_sessions == 0
    finally:
        await plugin.close()


async def test_fake_provider_connections_share_the_account_session() -> None:
    plugin = create_provider(ProviderContext(fake=True))
    try:
        await plugin.discover(CREDENTIALS)
        connections = [
            plugin.create_connection(_config("Hugo", FAKE_MOWER_ONLINE)),
            plugin.create_connection(_config("Greta", FAKE_MOWER_OFFLINE)),
        ]
        assert all(isinstance(c, HusqvarnaConnection) for c in connections)
        for connection in connections:
            await connection.connect()
        assert plugin.pool.logins == 1
        assert plugin.pool.users(FAKE_APP_KEY, FAKE_APP_SECRET) == 2
        hugo, greta = connections
        assert (await hugo.get_state()).activity is MowerActivity.DOCKED
        assert (await greta.get_state()).activity is MowerActivity.OFFLINE
        outcome = await hugo.execute(ResumeMow())
        assert outcome.status is CommandStatus.ACCEPTED
        for connection in connections:
            await connection.disconnect()
    finally:
        await plugin.close()


def test_create_connection_refuses_missing_credentials() -> None:
    plugin = HusqvarnaProvider(create_provider(ProviderContext(fake=True)).pool)
    config = ConnectionConfig(mower_id=uuid4(), account_id=uuid4(), options={"iot_id": "x"})
    with pytest.raises(ValueError, match="application key and secret"):
        plugin.create_connection(config)


async def test_secrets_never_reach_logs_messages_or_reprs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    plugin = create_provider(ProviderContext(fake=True))
    try:
        with pytest.raises(CloudLoginRefused) as refused:
            await plugin.discover({"app_key": FAKE_APP_KEY, "app_secret": "very-wrong-secret"})
        await plugin.discover(CREDENTIALS)
        connection = plugin.create_connection(_config("Hugo", FAKE_MOWER_ONLINE))
        await connection.connect()
        await connection.execute(ResumeMow())
        lease = await plugin.pool.acquire(FAKE_APP_KEY, FAKE_APP_SECRET)
        texts = [
            caplog.text,
            str(refused.value),
            repr(connection),
            repr(lease.account),
            repr(lease.account.tokens),
            repr(lease.account.auth),
        ]
        await lease.release()
        await connection.disconnect()
    finally:
        await plugin.close()
    for text in texts:
        assert FAKE_APP_SECRET not in text
        assert "very-wrong-secret" not in text
        assert "fake-token-" not in text  # nor the bearer token
