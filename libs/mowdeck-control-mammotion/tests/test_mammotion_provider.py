"""The Mammotion cloud provider plugin (ADR 0034, ADR 0065) on the fake cloud."""

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from mowdeck_control import (
    CLOUD_PROVIDER_OPTION,
    CloudLoginRefused,
    CloudProviderPlugin,
    ConnectionConfig,
    ProviderContext,
)
from mowdeck_control.providers import installed_specs
from mowdeck_control_mammotion.cloud import MammotionCloudConnection
from mowdeck_control_mammotion.fake_cloud import FAKE_CLOUD_PASSWORD
from mowdeck_control_mammotion.provider import (
    DESCRIPTOR,
    SPEC,
    MammotionProvider,
    connection_options,
    create_provider,
)

ACCOUNT = "garden@fake.mowdeck.test"
CREDENTIALS = {"email": ACCOUNT, "password": FAKE_CLOUD_PASSWORD}


def test_spec_is_installed_tested_and_claims_unqualified_cloud_mowers() -> None:
    assert installed_specs()["mammotion"] is SPEC
    assert SPEC.enabled_by_default and SPEC.unqualified_cloud
    assert DESCRIPTOR.provider == "mammotion" and DESCRIPTOR.tested_on_hardware
    assert DESCRIPTOR.identity_field.key == "email"
    assert DESCRIPTOR.secret_keys == frozenset({"password"})
    assert not DESCRIPTOR.needs_developer_account


def test_connection_options_take_the_linked_account_email_as_account() -> None:
    linked = {CLOUD_PROVIDER_OPTION: "mammotion", "email": ACCOUNT, "password": "p"}
    assert connection_options(linked) == {"account": ACCOUNT, "password": "p"}
    own = {"account": "own@example.com", "credential_ref": "env:X"}
    assert connection_options(own) == own


async def test_fake_provider_discovers_and_reuses_the_session() -> None:
    plugin = create_provider(ProviderContext(fake=True))
    assert isinstance(plugin, CloudProviderPlugin)
    found = await plugin.discover(CREDENTIALS)
    assert [d.device_name for d in found.devices] == [
        "Luba-FAKE0001",
        "Luba-FAKE0002",
        "RTK-FAKE0001",
    ]
    assert not found.reused_session
    again = await plugin.discover(CREDENTIALS)
    assert again.reused_session and plugin.pool.logins == 1
    with pytest.raises(CloudLoginRefused):
        await plugin.discover({"email": ACCOUNT, "password": "wrong"})
    await plugin.close()
    assert plugin.pool.open_sessions == 0


async def test_fake_provider_connections_share_the_account_session() -> None:
    plugin = create_provider(ProviderContext(fake=True))
    connections = [
        plugin.create_connection(
            ConnectionConfig(
                mower_id=uuid4(),
                account_id=uuid4(),
                options={
                    CLOUD_PROVIDER_OPTION: "mammotion",
                    **CREDENTIALS,
                    "device_name": name,
                    "linked_account_id": str(uuid4()),
                },
            )
        )
        for name in ("Luba-FAKE0001", "Luba-FAKE0002")
    ]
    assert all(isinstance(c, MammotionCloudConnection) for c in connections)
    for connection in connections:
        await connection.port.open()  # type: ignore[attr-defined]
    assert plugin.pool.logins == 1
    assert plugin.pool.users(ACCOUNT, FAKE_CLOUD_PASSWORD) == 2
    for connection in connections:
        await connection.port.close()  # type: ignore[attr-defined]
    await plugin.close()


def test_real_provider_never_loads_the_fake_cloud(tmp_path: Path) -> None:
    sys.modules.pop("mowdeck_control_mammotion.fake_cloud", None)
    plugin = create_provider(ProviderContext(cache_dir=tmp_path))
    assert isinstance(plugin, MammotionProvider)
    cache = plugin.pool.cache_path("a@example.com")
    assert cache is not None and cache.parent == tmp_path
    assert "mowdeck_control_mammotion.fake_cloud" not in sys.modules
    create_provider(ProviderContext(fake=True))
    assert "mowdeck_control_mammotion.fake_cloud" in sys.modules
