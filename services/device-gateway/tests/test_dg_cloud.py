"""Cloud account discovery and the shared cloud session per account (ADR 0034).

The fake Mammotion cloud of ``mowdeck-control-mammotion`` stands in for the vendor: its
accounts are ``*@fake.mowdeck.test`` with the password ``fake-cloud-password``.
"""

import asyncio
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from dg_support import SilentFleet, SilentPublisher, make_settings, telemetry, wait_until
from fastapi import FastAPI
from mowdeck_contracts.v1 import (
    CloudDiscoverErrorCode,
    CloudDiscoverReply,
    CloudDiscoverRequest,
    CloudProvider,
    ConnectionKind,
    MowerCredentialsReply,
    MowerCredentialsRequest,
    MowerRegistration,
)
from mowdeck_contracts.v1.subjects import FLEET_MOWERS_CREDENTIALS, GATEWAY_CLOUD_DISCOVER
from mowdeck_control_mammotion.fake_cloud import FAKE_CLOUD_PASSWORD
from mowdeck_service_kit.bus import BusMessage, InMemoryBus, RequestTimeoutError
from mowdeck_service_kit.testing import lifespan_client
from pydantic import ValidationError

from device_gateway.adapters.control.cloud_accounts import CloudBackend, cloud_backend
from device_gateway.adapters.control.connections import RegistryConnectionProvider
from device_gateway.application import gateway as gateway_module
from device_gateway.application.discovery import CloudDiscoveryService
from device_gateway.application.gateway import DeviceGateway
from device_gateway.application.ports import (
    CloudDevices,
    CloudLoginError,
    CloudUnavailableError,
)
from device_gateway.application.session import SessionTiming
from device_gateway.domain.rules import Shard
from device_gateway.main import build_app
from device_gateway.settings import Settings

FAKE_ACCOUNT = "garden@fake.mowdeck.test"
ACCOUNT_ID = uuid4()


def _request(**changes: object) -> CloudDiscoverRequest:
    values: dict[str, object] = {
        "account_id": ACCOUNT_ID,
        "email": FAKE_ACCOUNT,
        "password": FAKE_CLOUD_PASSWORD,
    }
    values.update(changes)
    return CloudDiscoverRequest.model_validate(values)


async def _ask(bus: InMemoryBus, request: CloudDiscoverRequest) -> CloudDiscoverReply:
    reply = await bus.request(GATEWAY_CLOUD_DISCOVER, request, timeout=2.0)
    return reply.model(CloudDiscoverReply)


def _cloud_registration(device_name: str, **options: object) -> MowerRegistration:
    return MowerRegistration(
        mower_id=uuid4(),
        account_id=ACCOUNT_ID,
        name=device_name,
        connection_kind=ConnectionKind.CLOUD,
        connection_options={
            "account": FAKE_ACCOUNT,
            "password": FAKE_CLOUD_PASSWORD,
            "device_name": device_name,
            "linked_account_id": str(uuid4()),
            **options,
        },
    )


def _app(bus: InMemoryBus, backend: CloudBackend, *registrations: MowerRegistration) -> FastAPI:
    settings = make_settings(
        bootstrap_mowers=[r.model_dump(mode="json") for r in registrations],
        fake_mammotion_cloud=True,
    )
    provider = RegistryConnectionProvider(sim_tick_seconds=0.05, cloud_factory=backend.factory)
    return build_app(
        settings, bus=bus, connection_provider=provider, fleet=SilentFleet(), cloud=backend
    )


async def test_dg_cloud_discovery_lists_the_devices_of_the_account() -> None:
    bus = InMemoryBus()
    backend = cloud_backend(fake=True)
    async with lifespan_client(_app(bus, backend)):
        reply = await _ask(bus, _request())
        assert reply.error is None and reply.devices is not None
        names = [(d.device_name, d.is_mower, d.online) for d in reply.devices]
        assert names == [
            ("Luba-FAKE0001", True, True),
            ("Luba-FAKE0002", True, False),
            ("RTK-FAKE0001", False, True),
        ]
        assert reply.devices[0].model == "LUBA 2 AWD 5000X"
        assert not reply.reused_session
        again = await _ask(bus, _request(linked_account_id=uuid4()))
        assert again.reused_session and backend.pool.logins == 1  # the session lingers

        refused = await _ask(bus, _request(password="wrong"))  # noqa: S106 - a test value
        assert refused.error is not None
        assert refused.error.code is CloudDiscoverErrorCode.LOGIN_FAILED
        assert "wrong" not in refused.error.detail
        with pytest.raises(RequestTimeoutError):  # an invalid request is dropped, not answered
            await bus.request(GATEWAY_CLOUD_DISCOVER, b'{"email": "x"}', timeout=0.2)


async def test_dg_cloud_mowers_of_one_account_share_one_login() -> None:
    bus = InMemoryBus()
    backend = cloud_backend(fake=True)
    front, back = _cloud_registration("Luba-FAKE0001"), _cloud_registration("Luba-FAKE0002")
    async with lifespan_client(_app(bus, backend, front, back)):
        await wait_until(lambda: telemetry(bus, front.mower_id) and telemetry(bus, back.mower_id))
        assert backend.pool.logins == 1
        assert backend.pool.users(FAKE_ACCOUNT, FAKE_CLOUD_PASSWORD) == 2
        # A re-read of the account answers from the mowers' session: no second login.
        reply = await _ask(bus, _request())
        assert reply.reused_session and backend.pool.logins == 1
    assert backend.pool.open_sessions == 0  # shutdown stopped the shared session


async def test_dg_cloud_provider_uses_the_cloud_factory_only_for_cloud_mowers() -> None:
    backend = cloud_backend(fake=True)
    provider = RegistryConnectionProvider(cloud_factory=backend.factory)
    connection = provider.create(_cloud_registration("Luba-FAKE0001"))
    assert type(connection.port).__name__ == "PooledFakePort"  # type: ignore[attr-defined]
    simulated = provider.create(
        MowerRegistration(
            mower_id=uuid4(),
            account_id=ACCOUNT_ID,
            name="Sim",
            connection_kind=ConnectionKind.SIMULATED,
        )
    )
    assert simulated.kind is ConnectionKind.SIMULATED


def test_dg_real_backend_never_loads_the_fake_cloud(tmp_path: Path) -> None:
    sys.modules.pop("mowdeck_control_mammotion.fake_cloud", None)
    backend = cloud_backend(cache_dir=tmp_path)
    assert not backend.fake and backend.accounts.supports(CloudProvider.MAMMOTION)
    cache = backend.pool.cache_path("a@example.com")
    assert cache is not None and cache.parent == tmp_path
    assert "mowdeck_control_mammotion.fake_cloud" not in sys.modules
    assert cloud_backend(fake=True).fake
    assert "mowdeck_control_mammotion.fake_cloud" in sys.modules


class FakeFleetCredentials:
    """Answers ``fleet.mowers.credentials`` like Fleet."""

    def __init__(self, secrets: dict[UUID, dict[str, str]]) -> None:
        self.secrets = secrets
        self.requests: list[MowerCredentialsRequest] = []

    async def handle(self, message: BusMessage) -> None:
        request = message.model(MowerCredentialsRequest)
        self.requests.append(request)
        found = self.secrets.get(request.mower_id)
        await message.respond(
            MowerCredentialsReply(found=found is not None, secret_options=found or {})
        )


async def test_dg_fetches_withheld_credentials_for_each_connect() -> None:
    """Registrations carry no secrets: the gateway asks Fleet when it opens the connection."""
    bus = InMemoryBus()
    backend = cloud_backend(fake=True)
    front = _cloud_registration("Luba-FAKE0001")
    withheld = {
        k: v for k, v in front.connection_options.items() if k not in {"account", "password"}
    }
    front = front.model_copy(
        update={"connection_options": {**withheld, "credentials": "fleet", "credentials_rev": "1"}}
    )
    unknown = _cloud_registration("Luba-FAKE0002")
    unknown = unknown.model_copy(update={"connection_options": {"credentials": "fleet"}})
    fleet = FakeFleetCredentials(
        {front.mower_id: {"account": FAKE_ACCOUNT, "password": FAKE_CLOUD_PASSWORD}}
    )
    await bus.subscribe(FLEET_MOWERS_CREDENTIALS, fleet.handle, queue="fleet")
    async with lifespan_client(_app(bus, backend, front, unknown)) as client:
        await wait_until(lambda: telemetry(bus, front.mower_id))
        await wait_until(
            lambda: len([r for r in fleet.requests if r.mower_id == unknown.mower_id]) >= 2
        )
        held = client._transport.app.state.gateway.session(front.mower_id)  # type: ignore[attr-defined]
        assert held.registration == front  # the secrets were not kept on the session
        assert backend.pool.logins == 1
        assert not telemetry(bus, unknown.mower_id)  # no credentials, no connection
    assert {r.mower_id for r in fleet.requests} == {front.mower_id, unknown.mower_id}
    assert all(r.account_id == ACCOUNT_ID for r in fleet.requests)


async def test_dg_warns_once_about_linked_mowers_with_several_shards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(gateway_module._log, "warning", lambda event, **_: warnings.append(event))
    gateway = DeviceGateway(
        shard=Shard(0, 2),
        provider=RegistryConnectionProvider(cloud_factory=cloud_backend(fake=True).factory),
        publisher=SilentPublisher(),
        timing=SessionTiming(),
    )
    registered = 0
    while registered < 2:
        reg = _cloud_registration("Luba-FAKE0001")
        if gateway.shard.owns(reg.mower_id):
            await gateway.register(reg, source="test")
            registered += 1
    await gateway.close()
    assert sum("unsupported with several shards" in w for w in warnings) == 1


class StubAccounts:
    def __init__(self, error: Exception | None = None, *, supported: bool = True) -> None:
        self.error = error
        self.supported = supported
        self.delay = 0.0

    def supports(self, provider: CloudProvider) -> bool:
        return self.supported

    async def discover(self, provider: CloudProvider, email: str, password: str) -> CloudDevices:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return CloudDevices(devices=[], reused_session=False)


@pytest.mark.parametrize(
    ("accounts", "code"),
    [
        (StubAccounts(CloudLoginError("no")), CloudDiscoverErrorCode.LOGIN_FAILED),
        (StubAccounts(CloudUnavailableError("cloud down")), CloudDiscoverErrorCode.UNAVAILABLE),
        (StubAccounts(supported=False), CloudDiscoverErrorCode.UNSUPPORTED),
    ],
)
async def test_dg_cloud_discovery_errors(
    accounts: StubAccounts, code: CloudDiscoverErrorCode
) -> None:
    outcomes: list[str] = []
    service = CloudDiscoveryService(accounts, timeout_seconds=1, count=outcomes.append)
    reply = await service.discover(_request())
    assert reply.error is not None and reply.error.code is code
    assert outcomes == [code.value]


async def test_dg_cloud_discovery_runs_at_most_the_configured_logins_at_once() -> None:
    running = 0
    peak = 0

    class Slow(StubAccounts):
        async def discover(
            self, provider: CloudProvider, email: str, password: str
        ) -> CloudDevices:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            return CloudDevices(devices=[], reused_session=False)

    service = CloudDiscoveryService(Slow(), timeout_seconds=1, concurrency=2)
    replies = await asyncio.gather(*(service.discover(_request()) for _ in range(5)))
    assert peak == 2 and all(r.devices == [] for r in replies)


async def test_dg_cloud_discovery_times_out() -> None:
    accounts = StubAccounts()
    accounts.delay = 1.0
    reply = await CloudDiscoveryService(accounts, timeout_seconds=0.01).discover(_request())
    assert reply.error is not None and reply.error.code is CloudDiscoverErrorCode.UNAVAILABLE
    assert "did not answer" in reply.error.detail
    empty = await CloudDiscoveryService(StubAccounts(), timeout_seconds=1).discover(_request())
    assert empty.devices == []


def test_dg_fake_cloud_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="development only"):
        Settings.model_validate({"environment": "prod", "fake_mammotion_cloud": True})
    assert make_settings(fake_mammotion_cloud=True).fake_mammotion_cloud
