"""Cloud provider plugins: loading, enabling and routing (ADR 0066)."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint
from typing import Any
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import ProviderDescriptor
from mowdeck_control import (
    CLOUD_PROVIDER_OPTION,
    CloudProviderPlugin,
    ConnectionConfig,
    MowerConnection,
    ProviderContext,
    ProviderDevices,
    ProviderSpec,
    SimulatedMower,
    load_providers,
)
from mowdeck_control.providers import enabled_ids, installed_specs
from mowdeck_control.simulator import create_simulated_connection


def _descriptor(provider: str, name: str) -> ProviderDescriptor:
    return ProviderDescriptor.model_validate(
        {
            "provider": provider,
            "name": name,
            "fields": [
                {"key": "email", "kind": "email", "label": "E-mail", "identity": True},
                {"key": "password", "kind": "secret", "label": "Password"},
            ],
        }
    )


@dataclass
class FakePlugin:
    """A provider plugin that records what the gateway does with it."""

    provider: str
    name: str
    context: ProviderContext
    closed: bool = False
    fail_close: bool = False
    seen: list[dict[str, Any]] = field(default_factory=list)

    @property
    def descriptor(self) -> ProviderDescriptor:
        return _descriptor(self.provider, self.name)

    async def discover(self, credentials: Mapping[str, str]) -> ProviderDevices:
        return ProviderDevices(devices=[])

    def create_connection(self, config: ConnectionConfig) -> MowerConnection:
        self.seen.append(dict(config.options))
        return create_simulated_connection(ConnectionConfig(config.mower_id, config.account_id))

    async def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("boom")


def _spec(provider: str, name: str, **kwargs: Any) -> ProviderSpec:
    return ProviderSpec(provider, lambda ctx: FakePlugin(provider, name, ctx), **kwargs)


class _Point:
    """An entry point stand-in that returns a fixed object (or raises)."""

    def __init__(self, name: str, value: object) -> None:
        self.name = name
        self._value = value

    def load(self) -> object:
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


def _source(*points: _Point) -> Any:
    def source() -> Iterable[EntryPoint]:
        return points  # type: ignore[return-value]

    return source


def test_installed_specs_skip_broken_mismatched_and_duplicate_entry_points() -> None:
    good = _spec("acme", "Acme")
    found = installed_specs(
        _source(
            _Point("acme", good),
            _Point("acme", _spec("acme", "Second")),
            _Point("broken", ImportError("missing SDK")),
            _Point("other", _spec("zeta", "Zeta")),
            _Point("plain", object()),
        )
    )
    assert found == {"acme": good}


def test_enabled_ids_take_defaults_plus_enable_minus_disable() -> None:
    specs = {
        "tested": _spec("tested", "T", enabled_by_default=True),
        "beta": _spec("beta", "B"),
        "gamma": _spec("gamma", "G"),
    }
    assert enabled_ids(specs) == ["tested"]
    assert enabled_ids(specs, enable=["beta", "unknown"]) == ["beta", "tested"]
    assert enabled_ids(specs, enable=["beta"], disable=["tested", "beta"]) == []


async def test_load_providers_creates_enabled_plugins_with_the_context() -> None:
    context = ProviderContext(fake=True)
    loaded = load_providers(
        context,
        enable=["beta"],
        source=_source(
            _Point("tested", _spec("tested", "Zulu Cloud", enabled_by_default=True)),
            _Point("beta", _spec("beta", "Alpha Cloud")),
            _Point("off", _spec("off", "Off Cloud")),
        ),
    )
    assert sorted(loaded.plugins) == ["beta", "tested"]
    assert [d.name for d in loaded.descriptors()] == ["Alpha Cloud", "Zulu Cloud"]
    plugin = loaded.plugins["beta"]
    assert isinstance(plugin, CloudProviderPlugin)
    assert isinstance(plugin, FakePlugin) and plugin.context is context
    assert loaded.unqualified_cloud is None


async def test_load_providers_leaves_out_failing_and_mislabelled_plugins() -> None:
    def explode(_: ProviderContext) -> CloudProviderPlugin:
        raise RuntimeError("no SDK")

    loaded = load_providers(
        ProviderContext(),
        enable=["broken", "liar"],
        source=_source(
            _Point("broken", ProviderSpec("broken", explode)),
            _Point("liar", ProviderSpec("liar", lambda ctx: FakePlugin("other", "X", ctx))),
        ),
    )
    assert loaded.plugins == {}


async def test_routing_of_cloud_registrations_and_close() -> None:
    loaded = load_providers(
        ProviderContext(),
        enable=["acme", "zeta", "older"],
        source=_source(
            _Point("acme", _spec("acme", "Acme")),
            _Point("zeta", _spec("zeta", "Zeta", unqualified_cloud=True)),
            _Point("older", _spec("older", "Older", unqualified_cloud=True)),
        ),
    )
    # Two claims: the first by id wins (a warning is logged).
    assert loaded.unqualified_cloud == "older"
    assert loaded.for_registration({CLOUD_PROVIDER_OPTION: "acme"}) is loaded.plugins["acme"]
    assert loaded.for_registration({}) is loaded.plugins["older"]
    assert loaded.for_registration({CLOUD_PROVIDER_OPTION: "missing"}) is None
    plugin = loaded.plugins["acme"]
    assert isinstance(plugin, FakePlugin)
    connection = plugin.create_connection(
        ConnectionConfig(mower_id=uuid4(), account_id=uuid4(), options={"email": "a@x.test"})
    )
    assert isinstance(connection, SimulatedMower)
    assert plugin.seen == [{"email": "a@x.test"}]
    zeta = loaded.plugins["zeta"]
    assert isinstance(zeta, FakePlugin)
    zeta.fail_close = True
    await loaded.close()
    assert all(isinstance(p, FakePlugin) and p.closed for p in loaded.plugins.values())


def test_routing_without_any_claim() -> None:
    loaded = load_providers(
        ProviderContext(), enable=["acme"], source=_source(_Point("acme", _spec("acme", "A")))
    )
    assert loaded.for_registration({}) is None


@pytest.mark.parametrize("value", [None, "", 3])
def test_routing_ignores_empty_provider_options(value: object) -> None:
    loaded = load_providers(
        ProviderContext(),
        enable=["acme"],
        source=_source(_Point("acme", _spec("acme", "A", unqualified_cloud=True))),
    )
    assert loaded.for_registration({CLOUD_PROVIDER_OPTION: value}) is loaded.plugins["acme"]
