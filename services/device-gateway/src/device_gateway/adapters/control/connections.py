"""Creates mower connections through the ``mowdeck-control`` registry.

Adapters come from the registry (``simulated`` is built in; the GPL package
``mowdeck-control-mammotion`` adds ``ble_bridge`` and ``cloud`` through entry points).
Simulated mowers get their clock here: :meth:`SimulatedMower.run` ticks in real time with the
configured time scale.

Cloud mowers (ADR 0034): with a ``cloud_factory`` (``main`` passes the one of
:mod:`device_gateway.adapters.control.cloud_accounts`) every ``cloud`` connection borrows the
shared session of its Mammotion account from one pool instead of logging in per mower; each
mower still gets its own connection, port and controller.
"""

from collections.abc import Sequence
from typing import Any

from mowdeck_contracts.v1 import BASE_STATION_OPTION, ConnectionKind, MowerRegistration
from mowdeck_control import (
    ConnectionConfig,
    ConnectionFactory,
    ConnectionRegistry,
    MowerConnection,
    SimulatedMower,
    default_registry,
)

from device_gateway.application.ports import BackgroundJob


class SimulatedOnlyError(ValueError):
    """A real mower in a demo installation (ADR 0065): it is never connected."""


class RegistryConnectionProvider:
    """:class:`~device_gateway.application.ports.ConnectionProvider` on the control registry."""

    def __init__(
        self,
        *,
        sim_time_scale: float = 1.0,
        sim_tick_seconds: float = 1.0,
        registry: ConnectionRegistry | None = None,
        cloud_factory: ConnectionFactory | None = None,
        simulated_only: bool = False,
    ) -> None:
        self._sim_time_scale = sim_time_scale
        self._sim_tick_seconds = sim_tick_seconds
        self._registry = registry or default_registry()
        self._cloud_factory = cloud_factory
        self._simulated_only = simulated_only
        """Public demo (ADR 0065): any other kind is refused before an adapter is touched."""

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
        if registration.connection_kind is ConnectionKind.CLOUD and self._cloud_factory:
            return self._cloud_factory(config)
        return self._registry.create(registration.connection_kind, config)

    def background_jobs(self, connection: MowerConnection) -> Sequence[BackgroundJob]:
        """The simulation clock for simulated mowers; nothing for real links."""
        if isinstance(connection, SimulatedMower):
            mower = connection
            tick = self._sim_tick_seconds

            async def run_simulation() -> None:
                await mower.run(tick)

            return [run_simulation]
        return []
