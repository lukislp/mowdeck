"""The ``cloud`` connection of one Worx mower (a thin translator, ADR 0012).

The connection borrows its account's shared session from the
:class:`~mowdeck_control_worx.pool.AccountPool` and maps every snapshot of its mower onto a
:class:`~mowdeck_contracts.v1.MowerState` (:mod:`~mowdeck_control_worx.mapping`). Safety rules
(expiry, de-duplication, emergency stop priority, capability checks) stay in
:class:`~mowdeck_control.MowerController`.

Commands go over MQTT; the mower confirms them through its next status, so a sent command is
``accepted``:

==============================  ==========================================================
MowDeck                         Worx cloud (pyworxcloud)
==============================  ==========================================================
``StartMow`` ``plan_ref``       ``now``: start; ``zone:<1-4>``: set the zone, then start;
                                ``edge``: edge cut (the declared ``start_plans``)
``StartMow`` areas / spot plan  rejected: the cloud has no map geometry, no custom paths
``PauseMow``                    pause
``ResumeMow``                   start (continues the paused job)
``ReturnToDock``                home (ends the job either way; the next one follows the
                                mower's schedule)
``EmergencyStop``               pause at once (the cloud has no hard stop)
``SetBladeHeight``              cutting height, models with the cutting height module
``SetSetting`` ``rain_delay``   rain delay in minutes
everything else                 rejected (speed, light, firmware, spot runs, driving)
==============================  ==========================================================

A start or resume during an error raises :class:`~mowdeck_control.SensorStop` (a lift is never
cleared by a command); a command for a mower the cloud reports offline is rejected (the
emergency stop is sent anyway).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Final

from mowdeck_contracts.v1 import (
    ConnectionKind,
    EmergencyStop,
    GeoPosition,
    MowerActivity,
    MowerCapabilities,
    MowerMap,
    MowerState,
    PauseMow,
    ResumeMow,
    ReturnToDock,
    SetBladeHeight,
    SettingValue,
    StartMow,
)
from mowdeck_control import (
    CloudLoginRefused,
    CloudUnavailable,
    CommandRejected,
    ConnectionConfig,
    DriveSession,
    JobCommand,
    NotConnected,
    Outcome,
    SensorStop,
    StateBroadcaster,
)

from mowdeck_control_worx import mapping
from mowdeck_control_worx.options import WorxOptions
from mowdeck_control_worx.pool import AccountLease, AccountPool
from mowdeck_control_worx.session import (
    ActionKind,
    CloudCommandError,
    CommandUnsupportedError,
    MowerOfflineError,
    NoAnswerError,
    WorxAction,
)
from mowdeck_control_worx.snapshot import WorxMower

_log = logging.getLogger("mowdeck_control_worx.connection")

NO_HARD_STOP: Final = (
    "pause sent at once; the Worx cloud has no hard stop, the mower stops its blades and "
    "wheels as for a pause"
)


def actions_for_plan(plan_ref: str, mower: WorxMower, *, offer_zones: bool) -> list[WorxAction]:
    """The actions of a ``StartMow`` with ``plan_ref``: only the declared start plans."""
    ref = plan_ref.strip()
    declared = {p.ref for p in mapping.start_plans_of(mower, offer_zones=offer_zones)}
    if ref not in declared:
        offered = ", ".join(sorted(declared))
        msg = f"unknown start plan {plan_ref!r}; this mower offers {offered}"
        raise CommandRejected(msg)
    if ref == mapping.PLAN_NOW:
        return [WorxAction(ActionKind.START)]
    if ref == mapping.PLAN_EDGE:
        return [WorxAction(ActionKind.EDGE_CUT)]
    zone = int(ref.partition(":")[2]) - 1
    return [WorxAction(ActionKind.ZONE, zone), WorxAction(ActionKind.START)]


class WorxConnection:
    """:class:`~mowdeck_control.MowerConnection` of one mower of a linked Worx account (also
    :class:`~mowdeck_control.SettingsCapable` and :class:`~mowdeck_control.MapRebuildable`)."""

    def __init__(self, pool: AccountPool, config: ConnectionConfig, options: WorxOptions) -> None:
        self._pool = pool
        self._config = config
        self._options = options
        self._lock = asyncio.Lock()
        self._lease: AccountLease | None = None
        self._unlisten: Callable[[], None] | None = None
        self._serial: str | None = None
        self._state: MowerState | None = None
        self._reference: GeoPosition | None = None
        self._broadcaster = StateBroadcaster()

    def __repr__(self) -> str:
        return f"WorxConnection(mower={self._config.mower_id}, serial={self._serial})"

    @property
    def kind(self) -> ConnectionKind:
        return ConnectionKind.CLOUD

    @property
    def is_connected(self) -> bool:
        """True while the account session is held. A dropped MQTT link is not a lost
        connection: pyworxcloud reconnects it by itself, and a new login per gateway reconnect
        is what gets accounts blocked."""
        return self._lease is not None and self._lease.session.alive

    @property
    def serial_number(self) -> str | None:
        """The mower's serial number once connected."""
        return self._serial

    def _mower(self) -> WorxMower | None:
        lease, serial = self._lease, self._serial
        if lease is None or serial is None:
            return None
        return lease.session.mowers().get(serial)

    def _pick(self, mowers: Mapping[str, WorxMower]) -> str | None:
        wanted = self._options.serial_number
        if wanted is not None and wanted in mowers:
            return wanted
        name = self._options.device_name
        if name is None:
            return None
        names = mapping.device_names(mowers)
        return next((serial for serial, shown in names.items() if shown == name), None)

    async def connect(self) -> None:
        async with self._lock:
            if self._lease is not None and self._lease.session.alive:
                return
            if self._lease is not None:
                await self._drop_lease()
            try:
                lease = await self._pool.acquire(self._options.login)
            except (CloudLoginRefused, CloudUnavailable) as exc:
                raise NotConnected(str(exc)) from exc
            serial = self._pick(lease.session.mowers())
            if serial is None:
                await lease.release()
                msg = f"the mower is not on this {self._options.brand_name} account (any more)"
                raise NotConnected(msg)
            self._lease = lease
            self._serial = serial
            self._unlisten = lease.session.listen(self._on_data)
            self._on_data(lease.session.mowers())

    async def _drop_lease(self) -> None:
        unlisten, self._unlisten = self._unlisten, None
        if unlisten is not None:
            unlisten()
        lease, self._lease = self._lease, None
        if lease is not None:
            await lease.release()

    async def disconnect(self) -> None:
        async with self._lock:
            await self._drop_lease()
            self._broadcaster.close()

    def _on_data(self, mowers: Mapping[str, WorxMower]) -> None:
        lease, serial = self._lease, self._serial
        if lease is None or serial is None:
            return
        mower = mowers.get(serial)
        if mower is None:
            _log.warning("Worx mower %s left the account", self._config.mower_id)
            return
        state = mapping.state_of(
            mower,
            mower_id=self._config.mower_id,
            account_id=self._config.account_id,
            observed_at=lease.session.changed_at(serial),
            previous=self._state,
        )
        if self._reference is None and state.position is not None:
            self._reference = state.position
        if state != self._state:
            self._state = state
            self._broadcaster.publish(state)

    async def capabilities(self) -> MowerCapabilities:
        return mapping.capabilities_of(self._mower(), offer_zones=self._options.offer_zones)

    async def get_state(self) -> MowerState:
        if self._lease is None or self._state is None:
            raise NotConnected(f"not connected to the {self._options.brand_name} cloud")
        return self._state

    def watch_state(self) -> AsyncIterator[MowerState]:
        return self._broadcaster.subscribe(initial=self._state)

    async def get_map(self) -> MowerMap:
        return mapping.map_of(self._reference)

    def current_map(self) -> MowerMap | None:
        """The map with the geo reference learned since (:class:`~mowdeck_control.\
MapRebuildable`)."""
        return mapping.map_of(self._reference)

    async def start_drive_session(self) -> DriveSession:
        raise CommandRejected("manual driving is not supported by the Worx cloud")

    async def execute(self, command: JobCommand) -> Outcome:
        mower = self._mower()
        if mower is None or self._serial is None:
            raise NotConnected(f"not connected to the {self._options.brand_name} cloud")
        if isinstance(command, EmergencyStop):
            outcome = await self._send([WorxAction(ActionKind.PAUSE)])
            return Outcome.accepted(NO_HARD_STOP) if outcome is None else outcome
        if not mower.online:
            return Outcome.rejected(self._offline())
        actions = self._actions_for(command, mower)
        if actions is None:
            return Outcome.rejected(f"{command.kind} is not supported by the Worx adapter")
        outcome = await self._send(actions)
        if outcome is not None:
            return outcome
        return Outcome.accepted(f"{' + '.join(a.kind.value for a in actions)} sent")

    async def set_setting(self, key: str, value: SettingValue) -> Outcome:
        """:class:`~mowdeck_control.SettingsCapable`: the rain delay (the controller checked
        the key and the value against the declaration)."""
        mower = self._mower()
        if mower is None:
            raise NotConnected(f"not connected to the {self._options.brand_name} cloud")
        if key != mapping.RAIN_DELAY_SETTING or mower.rain_delay_min is None:
            raise CommandRejected(f"this mower has no setting {key}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise CommandRejected("the rain delay takes whole minutes")
        if not mower.online:
            return Outcome.rejected(self._offline())
        outcome = await self._send([WorxAction(ActionKind.RAIN_DELAY, value)])
        return outcome if outcome is not None else Outcome.accepted(f"rain delay {value} min sent")

    def _offline(self) -> str:
        return f"the {self._options.brand_name} cloud reports the mower offline"

    def _actions_for(self, command: JobCommand, mower: WorxMower) -> list[WorxAction] | None:
        starts = isinstance(command, StartMow | ResumeMow)
        if starts and mapping.activity_of(mower) is MowerActivity.ERROR:
            code, message, _ = mapping.error_of(mower, MowerActivity.ERROR)
            raise SensorStop(f"mower error {code or '-'}: {message or 'unknown'}")
        if isinstance(command, StartMow):
            if command.plan_ref is None:
                msg = (
                    "Worx mowers have no map areas or custom paths in MowDeck; start with one "
                    "of the mower's start plans"
                )
                raise CommandRejected(msg)
            return actions_for_plan(command.plan_ref, mower, offer_zones=self._options.offer_zones)
        if isinstance(command, PauseMow):
            return [WorxAction(ActionKind.PAUSE)]
        if isinstance(command, ResumeMow):
            return [WorxAction(ActionKind.START)]
        if isinstance(command, ReturnToDock):
            return [WorxAction(ActionKind.HOME)]
        if isinstance(command, SetBladeHeight):
            if not mower.has_cutting_height:
                raise CommandRejected("this mower has no cutting height module")
            low = mapping.BLADE_HEIGHT_RANGE_MM[0]
            if (command.mm - low) % mapping.BLADE_HEIGHT_STEP_MM:
                step = mapping.BLADE_HEIGHT_STEP_MM
                raise CommandRejected(f"the cutting height takes {step} mm steps from {low} mm")
            return [WorxAction(ActionKind.CUTTING_HEIGHT, command.mm)]
        return None

    async def _send(self, actions: list[WorxAction]) -> Outcome | None:
        """Send ``actions`` in order; None when all went out, else the outcome of the first
        that did not."""
        lease, serial = self._lease, self._serial
        if lease is None or serial is None:
            raise NotConnected(f"not connected to the {self._options.brand_name} cloud")
        for action in actions:
            try:
                async with asyncio.timeout(self._options.command_timeout_s):
                    await lease.session.send(serial, action)
            except MowerOfflineError:
                return Outcome.rejected(self._offline())
            except CommandUnsupportedError as exc:
                return Outcome.rejected(str(exc) or "this mower does not support the command")
            except (NoAnswerError, TimeoutError):
                return Outcome.failed(f"the mower did not answer {action.kind.value} in time")
            except CloudCommandError as exc:
                _log.warning("Worx command failed: %s", type(exc).__name__)
                return Outcome.failed(str(exc) or f"the {self._options.brand_name} cloud failed")
        return None
