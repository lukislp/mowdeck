"""A small configurable fake adapter: correct by default, broken on request.

Each flag breaks one contract, so the kit's tests can prove that the matching check catches it.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mowdeck_contracts.v1 import (
    ConnectionKind,
    GeoReference,
    MowerActivity,
    MowerCapabilities,
    MowerMap,
    MowerState,
    SettingGroup,
    SettingSpec,
    SettingType,
    SettingValue,
    StartMow,
    StartPlan,
    StartPlanKind,
)
from mowdeck_control import (
    CommandRejected,
    ConnectionConfig,
    JobCommand,
    Outcome,
    StateBroadcaster,
    StateSubscription,
)

RAIN = SettingSpec(key="rain_detection", type=SettingType.BOOL, group=SettingGroup.WEATHER)


@dataclass
class Breakage:
    """Which contract the fake breaks (all False: a conforming minimal adapter)."""

    light_declared_but_refused: bool = False
    settings_without_protocol: bool = False
    heartbeat_from_adapter: bool = False
    drive_session_not_implemented: bool = False
    slow_emergency_stop: bool = False
    no_start_offered: bool = False
    any_plan_accepted: bool = False
    watch_never_ends: bool = False
    wrong_kind_declared: bool = False


class FakeMower:
    """A cloud mower with one start plan, pause, resume, dock and the emergency stop."""

    def __init__(self, config: ConnectionConfig, breakage: Breakage | None = None) -> None:
        self.config = config
        self.broken = breakage or Breakage()
        self.connected = False
        self.activity = MowerActivity.DOCKED
        self.broadcaster = StateBroadcaster()
        self.lock = asyncio.Lock()
        self.executed: list[str] = []

    @property
    def kind(self) -> ConnectionKind:
        return ConnectionKind.CLOUD

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        if not self.broken.watch_never_ends:
            self.broadcaster.close()

    async def capabilities(self) -> MowerCapabilities:
        kind = ConnectionKind.SIMULATED if self.broken.wrong_kind_declared else self.kind
        plans = (
            [] if self.broken.no_start_offered else [StartPlan(ref="now", kind=StartPlanKind.NOW)]
        )
        return MowerCapabilities(
            connection_kinds=frozenset({kind}),
            start_plans=plans,
            light=self.broken.light_declared_but_refused,
            settings=[RAIN] if self.broken.settings_without_protocol else [],
        )

    def _state(self) -> MowerState:
        return MowerState(
            mower_id=self.config.mower_id,
            account_id=self.config.account_id,
            observed_at=datetime.now(UTC),
            heartbeat_at=datetime.now(UTC) if self.broken.heartbeat_from_adapter else None,
            activity=self.activity,
            battery_percent=80,
            connection=self.kind,
        )

    async def get_state(self) -> MowerState:
        return self._state()

    def watch_state(self) -> StateSubscription:
        return self.broadcaster.subscribe(initial=self._state())

    async def get_map(self) -> MowerMap:
        return MowerMap(reference=GeoReference(origin_lat=0, origin_lon=0))

    async def execute(self, command: JobCommand) -> Outcome:
        if command.kind == "emergency_stop":
            if self.broken.slow_emergency_stop:
                async with self.lock:
                    pass
            self.activity = MowerActivity.PAUSED
            return Outcome.executed()
        async with self.lock:
            if self.broken.slow_emergency_stop:
                await asyncio.sleep(1.0)
            return self._run(command)

    def _run(self, command: JobCommand) -> Outcome:
        self.executed.append(command.kind)
        if isinstance(command, StartMow):
            if command.plan_ref != "now" and not self.broken.any_plan_accepted:
                raise CommandRejected(f"unknown plan {command.plan_ref!r}")
            self.activity = MowerActivity.MOWING
        elif command.kind == "pause_mow":
            self.activity = MowerActivity.PAUSED
        elif command.kind == "resume_mow":
            self.activity = MowerActivity.MOWING
        elif command.kind == "return_to_dock":
            self.activity = MowerActivity.RETURNING
        elif command.kind == "set_light":
            return Outcome.rejected("the light is not offered after all")
        else:
            raise CommandRejected(f"{command.kind} is not offered")
        self.broadcaster.publish(self._state())
        return Outcome.executed()

    async def start_drive_session(self) -> Any:
        if self.broken.drive_session_not_implemented:
            raise NotImplementedError
        raise CommandRejected("no manual driving")


class FakeSettingsMower(FakeMower):
    """Declares one setting and implements ``SettingsCapable``; ``confirms`` False reports
    ``executed`` without the state showing the value (a broken read back)."""

    def __init__(self, config: ConnectionConfig, *, confirms: bool = True) -> None:
        super().__init__(config)
        self.confirms = confirms
        self.settings: dict[str, SettingValue] = {"rain_detection": False}

    async def capabilities(self) -> MowerCapabilities:
        caps = await super().capabilities()
        return caps.model_copy(update={"settings": [RAIN]})

    def _state(self) -> MowerState:
        return super()._state().model_copy(update={"settings": dict(self.settings)})

    async def set_setting(self, key: str, value: SettingValue) -> Outcome:
        if self.confirms:
            self.settings[key] = value
        return Outcome.executed()
