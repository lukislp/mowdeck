"""The ``cloud`` connection of one Husqvarna mower (a thin translator, ADR 0012).

The connection borrows its account's shared session from the
:class:`~mowdeck_control_husqvarna.pool.AccountPool` and maps every snapshot of its mower onto a
:class:`~mowdeck_contracts.v1.MowerState` (:mod:`~mowdeck_control_husqvarna.mapping`). Safety
rules (expiry, de-duplication, emergency stop priority, capability checks) stay in
:class:`~mowdeck_control.MowerController`.

Commands (``POST /mowers/{id}/actions``; the API answers "accepted" and the state confirms it
later, so a sent command is ``accepted``):

==============================  ==========================================================
MowDeck                         Automower Connect
==============================  ==========================================================
``StartMow`` ``plan_ref``       ``schedule``: ResumeSchedule; ``duration[:<minutes>]``:
                                Start for the minutes (default: option ``start_minutes``);
                                ``work_area:<id>[:<minutes>]``: StartInWorkArea
``StartMow`` areas / spot plan  rejected: the API has no map geometry, so the map has no
                                areas, and no custom paths
``PauseMow``                    Pause
``ResumeMow``                   ResumeSchedule (the mower follows its schedule again; outside
                                a schedule window it stays parked)
``ReturnToDock``                ParkUntilNextSchedule; with ``end_job``
                                ParkUntilFurtherNotice
``EmergencyStop``               Pause at once (no throttle wait); the API has no hard stop
everything else                 rejected (blade height, speed, light, firmware, spot runs)
==============================  ==========================================================

A start or resume during a sensor error raises :class:`~mowdeck_control.SensorStop`; a command
for a mower that is not connected to the Husqvarna cloud is rejected (the emergency stop is sent
anyway).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Final

from aioautomower.exceptions import (
    ApiBadRequestError,
    ApiError,
    ApiForbiddenError,
    ApiUnauthorizedError,
    FeatureNotSupportedError,
    HusqvarnaAutomowerError,
)
from aioautomower.model import MowerAttributes
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

from mowdeck_control_husqvarna import mapping
from mowdeck_control_husqvarna.account import ActionKind, MowerAction
from mowdeck_control_husqvarna.auth import RateLimitedError, urgent
from mowdeck_control_husqvarna.options import HusqvarnaOptions, minutes_problem
from mowdeck_control_husqvarna.pool import AccountLease, AccountPool

_log = logging.getLogger("mowdeck_control_husqvarna.connection")

PLAN_SCHEDULE: Final = mapping.PLAN_SCHEDULE
PLAN_DURATION: Final = mapping.PLAN_DURATION
PLAN_WORK_AREA: Final = mapping.PLAN_WORK_AREA

NO_HARD_STOP: Final = (
    "pause sent at once; the Automower Connect API has no hard stop, the mower stops its "
    "blades and wheels as for a pause"
)
RATE_LIMITED: Final = (
    "Husqvarna's rate limit is reached (1 request per second, 10,000 a month per application "
    "key); try again later"
)


def _minutes(text: str) -> int:
    try:
        minutes = int(text)
    except ValueError:
        raise CommandRejected(f"{text!r} is no number of minutes") from None
    problem = minutes_problem(minutes)
    if problem is not None:
        raise CommandRejected(problem)
    return minutes


def action_for_plan(
    plan_ref: str, attributes: MowerAttributes, default_minutes: int
) -> MowerAction:
    """The action of a ``StartMow`` with ``plan_ref`` (see the module docstring)."""
    kind, _, rest = plan_ref.strip().partition(":")
    if kind == PLAN_SCHEDULE and not rest:
        return MowerAction(ActionKind.RESUME_SCHEDULE)
    if kind == PLAN_DURATION:
        minutes = _minutes(rest) if rest else default_minutes
        return MowerAction(ActionKind.START_FOR, minutes=minutes)
    if kind == PLAN_WORK_AREA and rest:
        area, _, duration = rest.partition(":")
        if not attributes.capabilities.work_areas or not attributes.work_areas:
            raise CommandRejected("this mower has no work areas")
        try:
            area_id = int(area)
        except ValueError:
            raise CommandRejected(f"unknown work area {area!r}") from None
        if area_id not in attributes.work_areas:
            raise CommandRejected(f"unknown work area {area_id}")
        minutes = _minutes(duration) if duration else default_minutes
        return MowerAction(ActionKind.START_IN_WORK_AREA, minutes=minutes, work_area_id=area_id)
    msg = (
        f"unknown plan reference {plan_ref!r}; use {PLAN_SCHEDULE!r}, "
        f"'{PLAN_DURATION}[:<minutes>]' or '{PLAN_WORK_AREA}:<id>[:<minutes>]'"
    )
    raise CommandRejected(msg)


class HusqvarnaConnection:
    """:class:`~mowdeck_control.MowerConnection` of one mower of a linked Husqvarna account."""

    def __init__(
        self, pool: AccountPool, config: ConnectionConfig, options: HusqvarnaOptions
    ) -> None:
        self._pool = pool
        self._config = config
        self._options = options
        self._lock = asyncio.Lock()
        self._lease: AccountLease | None = None
        self._unlisten: Callable[[], None] | None = None
        self._husqvarna_id: str | None = None
        self._state: MowerState | None = None
        self._reference: GeoPosition | None = None
        self._broadcaster = StateBroadcaster()

    def __repr__(self) -> str:
        return f"HusqvarnaConnection(mower={self._config.mower_id}, husqvarna={self._husqvarna_id})"

    @property
    def kind(self) -> ConnectionKind:
        return ConnectionKind.CLOUD

    @property
    def is_connected(self) -> bool:
        return self._lease is not None

    @property
    def husqvarna_id(self) -> str | None:
        """The Husqvarna mower id once connected."""
        return self._husqvarna_id

    def _attributes(self) -> MowerAttributes | None:
        lease, mower_id = self._lease, self._husqvarna_id
        if lease is None or mower_id is None:
            return None
        return lease.account.mowers.get(mower_id)

    def _pick(self, mowers: Mapping[str, MowerAttributes]) -> str | None:
        wanted = self._options.mower_id
        if wanted is not None and wanted in mowers:
            return wanted
        name = self._options.device_name
        if name is None:
            return None
        names = mapping.device_names(mowers)
        return next((mid for mid, shown in names.items() if shown == name), None)

    async def connect(self) -> None:
        async with self._lock:
            if self._lease is not None:
                return
            try:
                lease = await self._pool.acquire(self._options.app_key, self._options.app_secret)
            except (CloudLoginRefused, CloudUnavailable) as exc:
                raise NotConnected(str(exc)) from exc
            mower_id = self._pick(lease.account.mowers)
            if mower_id is None:
                await lease.release()
                msg = "the mower is not on this Husqvarna account (any more)"
                raise NotConnected(msg)
            self._lease = lease
            self._husqvarna_id = mower_id
            self._unlisten = lease.account.listen(self._on_data)
            self._on_data(lease.account.mowers)

    async def disconnect(self) -> None:
        async with self._lock:
            unlisten, self._unlisten = self._unlisten, None
            if unlisten is not None:
                unlisten()
            lease, self._lease = self._lease, None
            if lease is not None:
                await lease.release()
            self._broadcaster.close()

    def _on_data(self, mowers: Mapping[str, MowerAttributes]) -> None:
        lease, mower_id = self._lease, self._husqvarna_id
        if lease is None or mower_id is None:
            return
        attributes = mowers.get(mower_id)
        if attributes is None:
            _log.warning("Husqvarna mower %s left the account", self._config.mower_id)
            return
        state = mapping.state_of(
            attributes,
            mower_id=self._config.mower_id,
            account_id=self._config.account_id,
            observed_at=lease.account.changed_at(mower_id),
        )
        if self._reference is None and state.position is not None:
            self._reference = state.position
        if state != self._state:
            self._state = state
            self._broadcaster.publish(state)

    async def capabilities(self) -> MowerCapabilities:
        return mapping.capabilities_of(self._attributes())

    async def get_state(self) -> MowerState:
        if self._lease is None or self._state is None:
            raise NotConnected("not connected to the Husqvarna cloud")
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
        raise CommandRejected("manual driving is not supported by the Automower Connect API")

    async def execute(self, command: JobCommand) -> Outcome:
        attributes = self._attributes()
        if attributes is None or self._husqvarna_id is None:
            raise NotConnected("not connected to the Husqvarna cloud")
        if isinstance(command, EmergencyStop):
            return await self._send(MowerAction(ActionKind.PAUSE), emergency=True)
        if not attributes.metadata.connected:
            return Outcome.rejected("the mower is not connected to the Husqvarna cloud")
        action = self._action_for(command, attributes)
        if action is None:
            return Outcome.rejected(
                f"{command.kind} is not supported by the Husqvarna Automower adapter"
            )
        return await self._send(action)

    def _action_for(self, command: JobCommand, attributes: MowerAttributes) -> MowerAction | None:
        starts = isinstance(command, StartMow | ResumeMow)
        if starts and mapping.activity_of(attributes) is MowerActivity.ERROR:
            code = attributes.mower.error_code
            raise SensorStop(f"mower error {code}: {mapping.error_text(code)}")
        if isinstance(command, StartMow):
            if command.plan_ref is None:
                msg = (
                    "Husqvarna mowers have no map areas or custom paths in MowDeck; start with "
                    "a plan reference (schedule, duration or work area)"
                )
                raise CommandRejected(msg)
            return action_for_plan(command.plan_ref, attributes, self._options.start_minutes)
        if isinstance(command, PauseMow):
            return MowerAction(ActionKind.PAUSE)
        if isinstance(command, ResumeMow):
            return MowerAction(ActionKind.RESUME_SCHEDULE)
        if isinstance(command, ReturnToDock):
            kind = (
                ActionKind.PARK_UNTIL_FURTHER_NOTICE
                if command.end_job
                else ActionKind.PARK_UNTIL_NEXT_SCHEDULE
            )
            return MowerAction(kind)
        return None

    async def _send(self, action: MowerAction, *, emergency: bool = False) -> Outcome:
        lease, mower_id = self._lease, self._husqvarna_id
        if lease is None or mower_id is None:
            raise NotConnected("not connected to the Husqvarna cloud")
        try:
            if emergency:
                with urgent():
                    await lease.account.act(mower_id, action)
            else:
                await lease.account.act(mower_id, action)
        except ApiForbiddenError:
            return Outcome.rejected(
                "Husqvarna refused the command (403): the application may lack access to the "
                "Automower Connect API"
            )
        except ApiBadRequestError:
            return Outcome.rejected("Husqvarna refused the command for the mower's current state")
        except FeatureNotSupportedError:
            return Outcome.rejected("this mower does not support the command")
        except RateLimitedError:
            return Outcome.failed(RATE_LIMITED)
        except (ApiUnauthorizedError, CloudLoginRefused):
            return Outcome.failed(
                "Husqvarna refused the application credentials; re-authenticate the linked account"
            )
        except (ApiError, HusqvarnaAutomowerError, CloudUnavailable) as exc:
            _log.warning("Husqvarna command failed: %s", type(exc).__name__)
            return Outcome.failed("the Husqvarna cloud did not answer")
        if emergency:
            return Outcome.accepted(NO_HARD_STOP)
        return Outcome.accepted(f"{action.kind.value} sent")
