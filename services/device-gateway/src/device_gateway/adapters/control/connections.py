"""Creates mower connections through the ``mowdeck-control`` registry.

Adapters come from the registry (``simulated`` is built in; adapter packages add their kinds
through the entry point group ``mowdeck.connections``). Simulated mowers get their clock here:
:meth:`SimulatedMower.run` ticks in real time with the configured time scale.

Cloud mowers (ADR 0034, ADR 0066): with ``cloud_providers`` (``main`` passes the loaded plugins
of :mod:`device_gateway.adapters.control.providers`) a ``cloud`` registration goes to the plugin
of its linked account's provider (option ``cloud_provider``; registrations without it go to the
provider that claims them), so the mowers of one vendor account share that plugin's session;
each mower still gets its own connection and controller. Without a plugin the registry's
``cloud`` factory is used.
"""

from collections.abc import Sequence
from typing import Any

from mowdeck_contracts.v1 import BASE_STATION_OPTION, ConnectionKind, MowerRegistration
from mowdeck_control import (
    CLOUD_PROVIDER_OPTION,
    ConnectionConfig,
    ConnectionRegistry,
    LoadedProviders,
    MowerConnection,
    SimulatedMower,
    default_registry,
)
from mowdeck_control.errors import UnknownConnectionKind

from device_gateway.application.ports import BackgroundJob


class SimulatedOnlyError(ValueError):
    """A real mower in a demo installation (ADR 0068): it is never connected."""


class RegistryConnectionProvider:
    """:class:`~device_gateway.application.ports.ConnectionProvider` on the control registry."""

    def __init__(
        self,
        *,
        sim_time_scale: float = 1.0,
        sim_tick_seconds: float = 1.0,
        registry: ConnectionRegistry | None = None,
        cloud_providers: LoadedProviders | None = None,
        simulated_only: bool = False,
    ) -> None:
        self._sim_time_scale = sim_time_scale
        self._sim_tick_seconds = sim_tick_seconds
        self._registry = registry or default_registry()
        self._cloud_providers = cloud_providers
        self._simulated_only = simulated_only
        """Public demo (ADR 0068): any other kind is refused before an adapter is touched."""

    def create(self, registration: MowerRegistration) -> MowerConnection:
        """Create the connection; simulated mowers default to the configured time scale.

        The additional links of the registration (ADR 0038) travel in the option ``links``
        (``{kind: options}``, with the secrets the session merged), so an adapter that supports
        them opens them next to its primary connection. The linked RTK base station (ADR 0047)
        travels in the option ``base_station`` (a ``BaseStationRef`` as JSON).

        With ``simulated_only`` (demo mode) every other kind raises :class:`SimulatedOnlyError`.
        """
        if self._simulated_only and registration.connection_kind is not ConnectionKind.SIMULATED:
            msg = f"demo mode: {registration.connection_kind.value} connections are refused"
            raise SimulatedOnlyError(msg)
        options: dict[str, Any] = dict(registration.connection_options)
        if registration.links:
            options["links"] = {k: dict(v) for k, v in registration.links.items()}
        if registration.base_station is not None:
            options[BASE_STATION_OPTION] = registration.base_station.model_dump(mode="json")
        if registration.connection_kind is ConnectionKind.SIMULATED:
            options.setdefault("time_scale", self._sim_time_scale)
        config = ConnectionConfig(
            mower_id=registration.mower_id, account_id=registration.account_id, options=options
        )
        if registration.connection_kind is ConnectionKind.CLOUD and self._cloud_providers:
            plugin = self._cloud_providers.for_registration(options)
            if plugin is not None:
                return plugin.create_connection(config)
        named = options.get(CLOUD_PROVIDER_OPTION)
        if registration.connection_kind is ConnectionKind.CLOUD and named:
            # Never hand one vendor's credentials to another vendor's adapter.
            msg = f"the cloud provider {named} is not enabled in this Device-Gateway"
            raise UnknownConnectionKind(msg)
        return self._registry.create(registration.connection_kind, config)

    def cloud_provider(self, registration: MowerRegistration) -> str | None:
        """The provider id of a ``cloud`` registration: its option ``cloud_provider``, else the
        enabled provider that claims unqualified registrations; None for other kinds."""
        if registration.connection_kind is not ConnectionKind.CLOUD:
            return None
        options = registration.connection_options
        if self._cloud_providers is not None:
            return self._cloud_providers.provider_of(options)
        named = options.get(CLOUD_PROVIDER_OPTION)
        return named if isinstance(named, str) and named else None

    def background_jobs(self, connection: MowerConnection) -> Sequence[BackgroundJob]:
        """The simulation clock for simulated mowers; nothing for real links."""
        if isinstance(connection, SimulatedMower):
            mower = connection
            tick = self._sim_tick_seconds

            async def run_simulation() -> None:
                await mower.run(tick)

            return [run_simulation]
        return []
