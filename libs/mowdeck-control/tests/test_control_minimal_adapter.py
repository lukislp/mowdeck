"""A minimal adapter (ADR 0066): connect, state, start, pause, resume and dock, nothing else.

MowDeck's functions are the same for every provider; what a mower can do is what its adapter
declares in ``MowerCapabilities``. A vendor with few features must plug in with little code and
still get clean behaviour: everything it does not declare is refused by the controller before
the adapter is asked (no dead commands reach the vendor), and the adapter never needs a stub
for a feature it lacks.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandStatus,
    ConnectionKind,
    EmergencyStop,
    GeoReference,
    ManualDrive,
    MowerActivity,
    MowerCapabilities,
    MowerCommand,
    MowerMap,
    MowerState,
    PauseMow,
    PrincipalKind,
    PrincipalRef,
    ResumeMow,
    ReturnToDock,
    SetBladeHeight,
    SetLight,
    SetSpeed,
    StartMow,
)
from mowdeck_control import (
    CommandRejected,
    JobCommand,
    MowerController,
    Outcome,
    StateBroadcaster,
    StateSubscription,
)
from mowdeck_control.testing import ManualClock, ManualTimers

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
ACCOUNT = uuid4()


class MinimalMower:
    """Everything a minimal cloud adapter implements; capabilities are the defaults."""

    SUPPORTED = frozenset({"start_mow", "pause_mow", "resume_mow", "return_to_dock"})

    def __init__(self, mower_id: UUID) -> None:
        self.mower_id = mower_id
        self.calls: list[str] = []
        self.broadcaster = StateBroadcaster()
        self.connected = False

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
        self.broadcaster.close()

    async def capabilities(self) -> MowerCapabilities:
        return MowerCapabilities(connection_kinds=frozenset({ConnectionKind.CLOUD}))

    def _state(self) -> MowerState:
        return MowerState(
            mower_id=self.mower_id,
            account_id=ACCOUNT,
            observed_at=ManualClock()(),
            activity=MowerActivity.DOCKED,
            battery_percent=90,
            connection=ConnectionKind.CLOUD,
        )

    async def get_state(self) -> MowerState:
        return self._state()

    def watch_state(self) -> StateSubscription:
        return self.broadcaster.subscribe(initial=self._state())

    async def get_map(self) -> MowerMap:
        # The vendor API has no map geometry.
        return MowerMap(reference=GeoReference(origin_lat=0, origin_lon=0))

    async def execute(self, command: JobCommand) -> Outcome:
        self.calls.append(command.kind)
        if command.kind == "emergency_stop":
            return Outcome.executed()  # mapped to the vendor's fastest stop
        if command.kind not in self.SUPPORTED:
            raise CommandRejected(f"{command.kind} is not offered by this mower")
        return Outcome.executed()

    async def start_drive_session(self) -> Any:
        self.calls.append("drive_session")
        raise CommandRejected("this mower cannot be driven manually")


@pytest.fixture
async def setup() -> tuple[MowerController, MinimalMower]:
    mower_id = uuid4()
    clock = ManualClock()
    connection = MinimalMower(mower_id)
    controller = MowerController(
        connection, mower_id=mower_id, clock=clock, timer_factory=ManualTimers(clock)
    )
    await controller.start()
    return controller, connection


def _envelope(mower_id: UUID, command: MowerCommand) -> CommandEnvelope:
    return CommandEnvelope.create(
        mower_id=mower_id, command=command, issued_by=ISSUER, account_id=ACCOUNT
    )


async def test_the_basic_commands_reach_the_minimal_adapter(
    setup: tuple[MowerController, MinimalMower],
) -> None:
    controller, mower = setup
    for command in (
        StartMow(plan_ref="schedule"),
        PauseMow(),
        ResumeMow(),
        ReturnToDock(),
        EmergencyStop(),
    ):
        result = await controller.handle(_envelope(controller.mower_id, command))
        assert result.status is CommandStatus.EXECUTED, (command.kind, result)
    assert mower.calls == [
        "start_mow",
        "pause_mow",
        "resume_mow",
        "return_to_dock",
        "emergency_stop",
    ]


@pytest.mark.parametrize(
    "command",
    [
        ManualDrive(linear=0.3, angular=0.0, ttl_ms=500),
        SetBladeHeight(mm=40),
        SetSpeed(mps=0.4),
        SetLight(on=True),
        StartMow(area_ids=["front"]),
    ],
    ids=["manual_drive", "blade_height", "speed", "light", "unknown_area"],
)
async def test_undeclared_features_are_refused_before_the_adapter_is_asked(
    setup: tuple[MowerController, MinimalMower], command: MowerCommand
) -> None:
    controller, mower = setup
    result = await controller.handle(_envelope(controller.mower_id, command))
    assert result.status is CommandStatus.REJECTED, (command.kind, result)
    assert result.reason  # a readable reason for the user, not a crash
    assert mower.calls == []  # no dead command reached the vendor


async def test_a_minimal_adapter_offers_no_previews_and_no_drive_session(
    setup: tuple[MowerController, MinimalMower],
) -> None:
    controller, mower = setup
    assert controller.can_preview_path is False
    capabilities = await controller.capabilities()
    assert not capabilities.manual_drive and not capabilities.native_patterns
    assert capabilities.camera.value == "none"
    assert "drive_session" not in mower.calls
