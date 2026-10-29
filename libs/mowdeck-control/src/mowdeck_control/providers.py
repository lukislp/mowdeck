"""Cloud provider plugins: how a vendor adapter links accounts and connects their mowers (ADR 0066).

A vendor whose mowers are reached through its cloud ships a package (Apache-2.0, or GPL-3.0 when
its SDK is GPL) that registers a :class:`ProviderSpec` in the entry point group
``mowdeck.cloud_providers``; the entry point name is the provider id::

    [project.entry-points."mowdeck.cloud_providers"]
    acme = "mowdeck_control_acme.plugin:SPEC"

The spec should be importable without the vendor SDK (import it inside ``create``), so a
disabled provider costs nothing. The Device-Gateway loads the specs, creates a
:class:`CloudProviderPlugin` for every enabled one (:func:`load_providers`) and from then on
only talks to the plugin:

* ``descriptor``: the :class:`~mowdeck_contracts.v1.ProviderDescriptor` (credential fields,
  guidance, ``tested_on_hardware``), served to Fleet and the web app;
* ``discover(credentials)``: log in (or reuse an open session) and list the account's devices,
  raising :class:`CloudLoginRefused` or :class:`CloudUnavailable`;
* ``create_connection(config)``: the :class:`MowerConnection` of one mower of a linked account;
  ``config.options`` hold the mower's options plus every credential field of its linked
  account (merged by the gateway, never logged);
* ``close()``: stop shared sessions when the gateway stops.

The plugin is a thin translator like every connection: the safety rules stay in
:class:`~mowdeck_control.controller.MowerController`. Which providers are enabled is the
gateway's configuration (``ProviderSpec.enabled_by_default`` plus the operator's enable and
disable lists); the core services never name a vendor.
"""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Protocol, runtime_checkable

from mowdeck_contracts.v1 import DiscoveredDevice, ProviderDescriptor

from mowdeck_control.connection import ConnectionConfig, MowerConnection
from mowdeck_control.errors import ControlError

PROVIDER_ENTRY_POINT_GROUP = "mowdeck.cloud_providers"

CLOUD_PROVIDER_OPTION = "cloud_provider"
"""Option of a ``cloud`` registration naming the provider of its linked account (set by Fleet).
Registrations without it (cloud mowers from before ADR 0066) go to the provider whose spec says
``unqualified_cloud``."""

_log = logging.getLogger("mowdeck_control.providers")


class CloudLoginRefused(ControlError):  # noqa: N818 - reads like the vendor's answer
    """The vendor cloud refused the credentials. The message never holds them."""


class CloudUnavailable(ControlError):  # noqa: N818 - reads like the HTTP status it maps to
    """The vendor cloud did not answer or failed; the credentials may still be right. The
    message is shown to the user, so it holds no credentials and no vendor payload."""


@dataclass(frozen=True, slots=True)
class ProviderDevices:
    """The devices of a vendor account and whether an already open session answered."""

    devices: list[DiscoveredDevice]
    reused_session: bool = False


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """What the gateway hands every plugin when it creates it."""

    fake: bool = False
    """Development and browser tests only: answer from the package's fake cloud (fixed test
    accounts, fake mowers) instead of the vendor's. Refused by the gateway in production."""
    cache_dir: Path | None = None
    """A private, writable directory for session caches (one subdirectory per provider is the
    plugin's choice); None: no cache."""


@runtime_checkable
class CloudProviderPlugin(Protocol):
    """One enabled vendor cloud in the Device-Gateway."""

    @property
    def descriptor(self) -> ProviderDescriptor:
        """The provider's id, display name, credential fields and guidance."""
        ...

    async def discover(self, credentials: Mapping[str, str]) -> ProviderDevices:
        """The devices of the account, raising :class:`CloudLoginRefused` for refused
        credentials and :class:`CloudUnavailable` for everything else."""
        ...

    def create_connection(self, config: ConnectionConfig) -> MowerConnection:
        """The connection of one mower of a linked account (credentials in the options)."""
        ...

    async def close(self) -> None:
        """Stop every shared session (the gateway stops)."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """The object a package registers under ``mowdeck.cloud_providers``."""

    provider: str
    """The provider id; must equal the entry point name and ``descriptor.provider``."""
    create: Callable[[ProviderContext], CloudProviderPlugin]
    """Builds the plugin (imports the vendor SDK)."""
    enabled_by_default: bool = False
    """True only for providers tested on real hardware; operators enable the others."""
    unqualified_cloud: bool = False
    """Also serves ``cloud`` registrations that name no provider (from before ADR 0066). At most
    one enabled provider may say so."""


EntryPointSource = Callable[[], Iterable[EntryPoint]]


def _installed() -> Iterable[EntryPoint]:
    return entry_points(group=PROVIDER_ENTRY_POINT_GROUP)


def installed_specs(source: EntryPointSource = _installed) -> dict[str, ProviderSpec]:
    """Every :class:`ProviderSpec` of the installed packages by provider id. Entry points that
    fail to import, hold no spec or name another provider are logged and skipped."""
    found: dict[str, ProviderSpec] = {}
    for entry_point in source():
        try:
            spec = entry_point.load()
        except Exception:
            _log.exception("cannot load cloud provider entry point %s", entry_point)
            continue
        if not isinstance(spec, ProviderSpec) or spec.provider != entry_point.name:
            _log.warning("ignoring cloud provider entry point %s: no matching spec", entry_point)
            continue
        if spec.provider in found:
            _log.warning("ignoring a second cloud provider %s from %s", spec.provider, entry_point)
            continue
        found[spec.provider] = spec
    return found


def enabled_ids(
    specs: Mapping[str, ProviderSpec], *, enable: Iterable[str] = (), disable: Iterable[str] = ()
) -> list[str]:
    """The provider ids to load: the defaults plus ``enable`` minus ``disable`` (``disable``
    wins), only installed ones, sorted. Unknown ids in the lists are logged."""
    wanted = {p for p, s in specs.items() if s.enabled_by_default} | set(enable)
    for unknown in sorted((set(enable) | set(disable)) - set(specs)):
        _log.warning("cloud provider %s is configured but not installed", unknown)
    return sorted((wanted - set(disable)) & set(specs))


@dataclass(frozen=True, slots=True)
class LoadedProviders:
    """The enabled plugins by provider id and which one takes unqualified ``cloud`` mowers."""

    plugins: Mapping[str, CloudProviderPlugin]
    unqualified_cloud: str | None = None

    def descriptors(self) -> list[ProviderDescriptor]:
        """The descriptors, sorted by display name."""
        return sorted((p.descriptor for p in self.plugins.values()), key=lambda d: d.name.lower())

    def for_registration(self, options: Mapping[str, object]) -> CloudProviderPlugin | None:
        """The plugin of a ``cloud`` registration with these options (None: no plugin)."""
        named = options.get(CLOUD_PROVIDER_OPTION)
        key = named if isinstance(named, str) and named else self.unqualified_cloud
        return self.plugins.get(key) if key is not None else None

    async def close(self) -> None:
        """Close every plugin; a failing one does not stop the others."""
        for provider, plugin in self.plugins.items():
            try:
                await plugin.close()
            except Exception:
                _log.exception("closing cloud provider %s failed", provider)


def load_providers(
    context: ProviderContext,
    *,
    enable: Sequence[str] = (),
    disable: Sequence[str] = (),
    source: EntryPointSource = _installed,
) -> LoadedProviders:
    """Create the plugin of every enabled, installed provider. A plugin that fails to start or
    whose descriptor names another provider is logged and left out."""
    specs = installed_specs(source)
    plugins: dict[str, CloudProviderPlugin] = {}
    for provider in enabled_ids(specs, enable=enable, disable=disable):
        try:
            plugin = specs[provider].create(context)
        except Exception:
            _log.exception("cannot start cloud provider %s", provider)
            continue
        if plugin.descriptor.provider != provider:
            _log.warning("ignoring cloud provider %s: its descriptor names another", provider)
            continue
        plugins[provider] = plugin
    claims = sorted(p for p in plugins if specs[p].unqualified_cloud)
    if len(claims) > 1:
        _log.warning("several providers claim unqualified cloud mowers: %s", ", ".join(claims))
    return LoadedProviders(plugins=plugins, unqualified_cloud=claims[0] if claims else None)
