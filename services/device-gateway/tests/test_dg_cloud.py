"""Cloud providers, account discovery and the connections of linked mowers (ADR 0034, 0065).

The gateway knows no vendor: a test provider plugin (:class:`AcmeCloud`) stands in for every
adapter package. It owns one "session" per set of credentials, like the real plugins, so the
tests can count logins. The plugins of the real packages are tested in their own packages.
"""

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint
from typing import Any
from uuid import UUID, uuid4

import pytest
from dg_support import SilentFleet, SilentPublisher, make_settings, telemetry, wait_until
from fastapi import FastAPI
from mowdeck_contracts.v1 import (
    CloudDiscoverErrorCode,
    CloudDiscoverReply,
    CloudDiscoverRequest,
    CloudProvidersReply,
    CloudProvidersRequest,
    ConnectionKind,
    DeviceType,
    DiscoveredDevice,
    MowerCredentialsReply,
    MowerCredentialsRequest,
    MowerRegistration,
    ProviderDescriptor,
)
from mowdeck_contracts.v1.subjects import (
    FLEET_MOWERS_CREDENTIALS,
    GATEWAY_CLOUD_DISCOVER,
    GATEWAY_CLOUD_PROVIDERS,
)
from mowdeck_control import (
    CLOUD_PROVIDER_OPTION,
    CloudLoginRefused,
    CloudUnavailable,
    ConnectionConfig,
    LoadedProviders,
    MowerConnection,
    ProviderContext,
    ProviderDevices,
    ProviderSpec,
    load_providers,
)
from mowdeck_control.errors import UnknownConnectionKind
from mowdeck_control.simulator import create_simulated_connection
from mowdeck_service_kit.bus import BusMessage, InMemoryBus, RequestTimeoutError
from mowdeck_service_kit.testing import lifespan_client
from pydantic import ValidationError

from device_gateway.adapters.control import providers as providers_module
from device_gateway.adapters.control.connections import RegistryConnectionProvider
from device_gateway.adapters.control.providers import PluginCloudAccounts, cloud_providers
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

ACCOUNT = "garden@acme.test"
PASSWORD = "acme-password"  # noqa: S105 - a test value
ACCOUNT_ID = uuid4()

DESCRIPTOR = ProviderDescriptor.model_validate(
    {
        "provider": "acme",
        "name": "Acme Cloud",
        "fields": [
            {"key": "email", "kind": "email", "label": "E-mail", "identity": True},
            {"key": "password", "kind": "secret", "label": "Password"},
        ],
    }
)
DEVICES = [
    DiscoveredDevice(device_name="Acme-1", is_mower=True, online=True, iot_id="a1"),
    DiscoveredDevice(device_name="Acme-2", is_mower=True, online=False, iot_id="a2"),
    DiscoveredDevice(
        device_name="Acme-Base", is_mower=False, device_type=DeviceType.RTK_BASE, iot_id="b1"
    ),
]


@dataclass
class AcmeCloud:
    """A provider plugin with one shared session per set of credentials."""

    context: ProviderContext = field(default_factory=ProviderContext)
    sessions: dict[tuple[str, str], int] = field(default_factory=dict)
    logins: int = 0
    closed: bool = False
    fail: Exception | None = None

    @property
    def descriptor(self) -> ProviderDescriptor:
        return DESCRIPTOR

    def _login(self, email: str, password: str) -> bool:
        key = (email.lower(), password)
        if key in self.sessions:
            return True
        if email.lower() != ACCOUNT or password != PASSWORD:
            msg = "the Acme cloud refused the e-mail or password"
            raise CloudLoginRefused(msg)
        self.logins += 1
        self.sessions[key] = 0
        return False

    async def discover(self, credentials: Mapping[str, str]) -> ProviderDevices:
        if self.fail is not None:
            raise self.fail
        reused = self._login(credentials["email"], credentials["password"])
        return ProviderDevices(devices=list(DEVICES), reused_session=reused)

    def create_connection(self, config: ConnectionConfig) -> MowerConnection:
        options = dict(config.options)
        if options.get("email") is None or options.get("password") is None:
            msg = "no credentials in the options"
            raise CloudLoginRefused(msg)
        self._login(str(options["email"]), str(options["password"]))
        self.sessions[(str(options["email"]).lower(), str(options["password"]))] += 1
        return create_simulated_connection(ConnectionConfig(config.mower_id, config.account_id))

    async def close(self) -> None:
        self.closed = True
        self.sessions.clear()


def _loaded(plugin: AcmeCloud | None = None, *, claims: bool = False) -> LoadedProviders:
    plugin = plugin or AcmeCloud()
    return LoadedProviders(plugins={"acme": plugin}, unqualified_cloud="acme" if claims else None)


def _request(**changes: object) -> CloudDiscoverRequest:
    values: dict[str, object] = {
        "provider": "acme",
        "account_id": ACCOUNT_ID,
        "credentials": {"email": ACCOUNT, "password": PASSWORD},
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
            CLOUD_PROVIDER_OPTION: "acme",
            "email": ACCOUNT,
            "password": PASSWORD,
            "device_name": device_name,
            "linked_account_id": str(uuid4()),
            **options,
        },
    )


def _app(bus: InMemoryBus, loaded: LoadedProviders, *registrations: MowerRegistration) -> FastAPI:
    settings = make_settings(bootstrap_mowers=[r.model_dump(mode="json") for r in registrations])
    provider = RegistryConnectionProvider(sim_tick_seconds=0.05, cloud_providers=loaded)
    return build_app(
        settings, bus=bus, connection_provider=provider, fleet=SilentFleet(), cloud=loaded
    )


async def test_dg_cloud_providers_are_answered_on_the_bus() -> None:
    bus = InMemoryBus()
    async with lifespan_client(_app(bus, _loaded())):
        reply = await bus.request(GATEWAY_CLOUD_PROVIDERS, CloudProvidersRequest(), timeout=2.0)
        found = reply.model(CloudProvidersReply)
        assert [d.provider for d in found.providers] == ["acme"]
        assert found.providers[0].fields[1].key == "password"


async def test_dg_cloud_discovery_lists_the_devices_of_the_account() -> None:
    bus = InMemoryBus()
    plugin = AcmeCloud()
    async with lifespan_client(_app(bus, _loaded(plugin))):
        reply = await _ask(bus, _request())
        assert reply.error is None and reply.devices == DEVICES
        assert not reply.reused_session
        again = await _ask(bus, _request(linked_account_id=uuid4()))
        assert again.reused_session and plugin.logins == 1

        wrong = {"email": ACCOUNT, "password": "wrong"}
        refused = await _ask(bus, _request(credentials=wrong))
        assert refused.error is not None
        assert refused.error.code is CloudDiscoverErrorCode.LOGIN_FAILED
        assert "wrong" not in refused.error.detail
        with pytest.raises(RequestTimeoutError):  # an invalid request is dropped, not answered
            await bus.request(GATEWAY_CLOUD_DISCOVER, b'{"email": "x"}', timeout=0.2)
    assert plugin.closed  # shutdown closes the plugins


async def test_dg_cloud_discovery_maps_the_deprecated_email_and_password() -> None:
    plugin = AcmeCloud()
    service = CloudDiscoveryService(PluginCloudAccounts(_loaded(plugin)), timeout_seconds=1)
    reply = await service.discover(
        _request(credentials={}, email=f" {ACCOUNT} ", password=PASSWORD)
    )
    assert reply.devices == DEVICES and plugin.logins == 1


async def test_dg_cloud_discovery_refuses_unknown_fields_and_disabled_providers() -> None:
    service = CloudDiscoveryService(PluginCloudAccounts(_loaded()), timeout_seconds=1)
    extra = await service.discover(
        _request(credentials={"email": ACCOUNT, "password": PASSWORD, "token": "x"})
    )
    assert extra.error is not None and extra.error.code is CloudDiscoverErrorCode.LOGIN_FAILED
    assert "x" not in extra.error.detail.split()
    other = await service.discover(_request(provider="zeta"))
    assert other.error is not None and other.error.code is CloudDiscoverErrorCode.UNSUPPORTED


async def test_dg_cloud_mowers_of_one_account_share_one_session() -> None:
    bus = InMemoryBus()
    plugin = AcmeCloud()
    front, back = _cloud_registration("Acme-1"), _cloud_registration("Acme-2")
    async with lifespan_client(_app(bus, _loaded(plugin), front, back)):
        await wait_until(lambda: telemetry(bus, front.mower_id) and telemetry(bus, back.mower_id))
        assert plugin.logins == 1
        assert plugin.sessions[(ACCOUNT, PASSWORD)] == 2
        reply = await _ask(bus, _request())  # answered from the mowers' session
        assert reply.reused_session and plugin.logins == 1


def test_dg_cloud_registrations_are_routed_by_provider() -> None:
    plugin = AcmeCloud()
    provider = RegistryConnectionProvider(cloud_providers=_loaded(plugin, claims=True))
    provider.create(_cloud_registration("Acme-1"))
    # Without a provider option the claiming provider takes it (cloud mowers before ADR 0065).
    plain = _cloud_registration("Acme-2")
    options = {k: v for k, v in plain.connection_options.items() if k != CLOUD_PROVIDER_OPTION}
    provider.create(plain.model_copy(update={"connection_options": options}))
    assert plugin.sessions[(ACCOUNT, PASSWORD)] == 2
    simulated = provider.create(
        MowerRegistration(
            mower_id=uuid4(),
            account_id=ACCOUNT_ID,
            name="Sim",
            connection_kind=ConnectionKind.SIMULATED,
        )
    )
    assert simulated.kind is ConnectionKind.SIMULATED


def test_dg_cloud_mower_of_a_disabled_provider_is_never_handed_to_another() -> None:
    provider = RegistryConnectionProvider(cloud_providers=_loaded(claims=True))
    with pytest.raises(UnknownConnectionKind, match="zeta is not enabled"):
        provider.create(_cloud_registration("Zeta-1", **{CLOUD_PROVIDER_OPTION: "zeta"}))


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
    plugin = AcmeCloud()
    front = _cloud_registration("Acme-1")
    withheld = {k: v for k, v in front.connection_options.items() if k not in {"email", "password"}}
    front = front.model_copy(
        update={"connection_options": {**withheld, "credentials": "fleet", "credentials_rev": "1"}}
    )
    unknown = _cloud_registration("Acme-2")
    unknown = unknown.model_copy(
        update={"connection_options": {CLOUD_PROVIDER_OPTION: "acme", "credentials": "fleet"}}
    )
    fleet = FakeFleetCredentials({front.mower_id: {"email": ACCOUNT, "password": PASSWORD}})
    await bus.subscribe(FLEET_MOWERS_CREDENTIALS, fleet.handle, queue="fleet")
    async with lifespan_client(_app(bus, _loaded(plugin), front, unknown)) as client:
        await wait_until(lambda: telemetry(bus, front.mower_id))
        await wait_until(
            lambda: len([r for r in fleet.requests if r.mower_id == unknown.mower_id]) >= 2
        )
        held = client._transport.app.state.gateway.session(front.mower_id)  # type: ignore[attr-defined]
        assert held.registration == front  # the secrets were not kept on the session
        assert plugin.logins == 1
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
        provider=RegistryConnectionProvider(cloud_providers=_loaded()),
        publisher=SilentPublisher(),
        timing=SessionTiming(),
    )
    registered = 0
    while registered < 2:
        reg = _cloud_registration("Acme-1")
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

    def descriptors(self) -> list[ProviderDescriptor]:
        return [DESCRIPTOR] if self.supported else []

    def descriptor(self, provider: str) -> ProviderDescriptor | None:
        return DESCRIPTOR if self.supported and provider == "acme" else None

    async def discover(self, provider: str, credentials: Mapping[str, str]) -> CloudDevices:
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


@pytest.mark.parametrize(
    ("error", "code", "detail"),
    [
        (CloudLoginRefused("refused"), CloudDiscoverErrorCode.LOGIN_FAILED, "refused"),
        (CloudUnavailable("cloud is down"), CloudDiscoverErrorCode.UNAVAILABLE, "cloud is down"),
        # A plugin bug answers with its type only: its message may echo the request.
        (KeyError(PASSWORD), CloudDiscoverErrorCode.UNAVAILABLE, "KeyError"),
    ],
)
async def test_dg_plugin_errors_become_discovery_errors(
    error: Exception, code: CloudDiscoverErrorCode, detail: str
) -> None:
    plugin = AcmeCloud(fail=error)
    service = CloudDiscoveryService(PluginCloudAccounts(_loaded(plugin)), timeout_seconds=1)
    reply = await service.discover(_request())
    assert reply.error is not None and reply.error.code is code
    assert detail in reply.error.detail and PASSWORD not in reply.error.detail


async def test_dg_cloud_discovery_runs_at_most_the_configured_logins_at_once() -> None:
    running = 0
    peak = 0

    class Slow(StubAccounts):
        async def discover(self, provider: str, credentials: Mapping[str, str]) -> CloudDevices:
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


def test_dg_cloud_providers_follow_the_enable_and_disable_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[ProviderContext] = []

    def create(context: ProviderContext) -> AcmeCloud:
        created.append(context)
        return AcmeCloud(context)

    class Point:
        name = "acme"

        def load(self) -> object:
            return ProviderSpec("acme", create)

    def source() -> Iterable[EntryPoint]:
        return [Point()]  # type: ignore[list-item]

    real = load_providers

    def load(context: ProviderContext, **kwargs: Any) -> LoadedProviders:
        return real(context, source=source, **kwargs)

    monkeypatch.setattr(providers_module, "load_providers", load)
    assert cloud_providers().plugins == {}  # untested providers are off by default
    loaded = cloud_providers(fake=True, enable=("acme",))
    assert list(loaded.plugins) == ["acme"] and created[-1].fake
    assert cloud_providers(enable=("acme",), disable=("acme",)).plugins == {}
    settings = make_settings(cloud_providers_enable=" Acme, zeta,", cloud_providers_disable="zeta")
    assert settings.enabled_cloud_providers == ("acme", "zeta")
    assert settings.disabled_cloud_providers == ("zeta",)


def test_dg_fake_clouds_are_refused_in_production() -> None:
    for name in ("fake_clouds", "fake_mammotion_cloud"):
        with pytest.raises(ValidationError, match="development only"):
            Settings.model_validate({"environment": "prod", name: True})
        assert make_settings(**{name: True}).use_fake_clouds
    assert not make_settings().use_fake_clouds
