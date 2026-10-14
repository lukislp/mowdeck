"""Helpers for the vision tests (a plain module: the repository has no conftest.py).

``harness()`` builds the real app with a memory store, the in-memory OpenFGA fake, an in-memory
bus, access tokens minted with ``TestKeys``, the kit's fake Entitlements and a provider
factory that answers with fixed assessments. ``SimRig`` plays the Device-Gateway for one
simulated mower on the bus (commands through a real ``MowerController``, telemetry, the map
snapshot and photo requests); ``FakeFleet`` answers ``fleet.drive.*`` and
``fleet.commands.issue`` like Fleet and forwards issued commands to the mower.
"""

import asyncio
import base64
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from mowdeck_auth.testing import TestKeys, bearer
from mowdeck_authz import RelationshipTuple, account, mower, user
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    CommandAccepted,
    CommandEnvelope,
    CommandPriority,
    DriveError,
    DriveErrorCode,
    DriveSessionStarted,
    DriveStartReply,
    DriveStartRequest,
    DriveStopReply,
    DriveStopRequest,
    IssueCommandError,
    IssueCommandErrorCode,
    IssueCommandReply,
    IssueCommandRequest,
    MowerCapabilitiesSnapshot,
    MowerMapSnapshot,
    MowerState,
    PhotoReply,
    PhotoRequest,
    Principal,
    PrincipalKind,
    VisionTask,
    utc_now,
)
from mowdeck_contracts.v1.subjects import (
    FLEET_COMMANDS_ISSUE,
    FLEET_DRIVE_START,
    FLEET_DRIVE_STOP,
    command_subject,
    device_subject,
    photo_subject,
    telemetry_subject,
)
from mowdeck_control import MowerController, SimulatedMower, SimulationConfig
from mowdeck_service_kit.bus import (
    BusMessage,
    EventBus,
    InMemoryBus,
    Subscription,
    publish_command,
    publish_command_result,
)
from mowdeck_service_kit.testing import FakeEntitlements, lifespan_client

from vision.adapters.memory import MemoryStore
from vision.application.assist import VisionService
from vision.application.ports import VisionProvider
from vision.domain.calibration import Correspondences, CorrespondencesOut, FrameSize
from vision.domain.context import SceneContext
from vision.domain.errors import ProviderFailed
from vision.domain.models import (
    Assessment,
    AssessmentOut,
    Image,
    ProposalState,
    ProposedStep,
    ProviderConfig,
    RiskFlag,
    Situation,
    Usage,
)
from vision.main import build_app
from vision.settings import Settings

API = "/api/v1/vision"
T0 = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
SECRETS_KEY = base64.b64encode(bytes(range(32))).decode()

QUICK_STEPS = [
    ProposedStep(linear=-1.0, angular=0.0, seconds=0.8),
    ProposedStep(linear=0.0, angular=0.6, seconds=0.4),
]
"""Backs off 0.4 m (frees a stuck simulator) and turns, in 1.2 s."""


def answer(
    steps: list[ProposedStep] | None = None,
    *,
    situation: Situation = Situation.STUCK_ON_OBSTACLE,
    confidence: float = 0.9,
    risk: list[RiskFlag] | None = None,
) -> AssessmentOut:
    """A model answer for tests."""
    return AssessmentOut(
        situation=situation,
        confidence=confidence,
        proposal=QUICK_STEPS if steps is None else steps,
        rationale="Back off and turn.",
        risk=risk or [],
    )


class StaticProvider:
    """``VisionProvider`` answering with a fixed assessment (or failing)."""

    def __init__(self, config: ProviderConfig, factory: "StaticFactory") -> None:
        self.config = config
        self.factory = factory

    async def analyse(
        self, images: list[Image], context: SceneContext, task: VisionTask
    ) -> Assessment:
        self.factory.calls.append((self.config, len(images), context, task))
        if self.factory.delay:
            await asyncio.sleep(self.factory.delay)
        if self.factory.fail:
            raise ProviderFailed(self.factory.fail)
        result = self.factory.answers.get(task, self.factory.default)
        return Assessment(
            result,
            self.config.model,
            self.config.family,
            Usage(input_tokens=1200, output_tokens=150, cost_usd=0.0039, latency_ms=12),
        )

    async def correspond(
        self, left: Image, right: Image, left_size: FrameSize, right_size: FrameSize
    ) -> Correspondences:
        self.factory.correspondence_calls.append((self.config, left_size, right_size))
        if self.factory.delay:
            await asyncio.sleep(self.factory.delay)
        if self.factory.fail:
            raise ProviderFailed(self.factory.fail)
        return Correspondences(
            self.factory.correspondences,
            self.config.model,
            self.config.family,
            Usage(input_tokens=2400, output_tokens=300, cost_usd=0.0072, latency_ms=20),
        )


@dataclass
class StaticFactory:
    """``ProviderFactory`` of the tests: every model answers ``default`` (per task overridable)."""

    default: AssessmentOut = field(default_factory=answer)
    answers: dict[VisionTask, AssessmentOut] = field(default_factory=dict)
    fail: str | None = None
    delay: float = 0.0
    calls: list[tuple[ProviderConfig, int, SceneContext, VisionTask]] = field(default_factory=list)
    correspondences: CorrespondencesOut = field(default_factory=CorrespondencesOut)
    correspondence_calls: list[tuple[ProviderConfig, FrameSize, FrameSize]] = field(
        default_factory=list
    )

    def create(self, config: ProviderConfig) -> VisionProvider:
        return StaticProvider(config, self)


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests (environment independent)."""
    values: dict[str, Any] = {
        "environment": "test",
        "log_format": "console",
        "vision_default_enabled": True,
        "vision_default_model": "fake/recover",
        "vision_secrets_key": SECRETS_KEY,
        "vision_frame_rate_hz": 10.0,
        "vision_settle_seconds": 0.3,
        "vision_state_timeout_seconds": 2.0,
        **overrides,
    }
    return Settings.model_validate(values)


class SimRig:
    """The Device-Gateway for one simulated mower on the in-memory bus."""

    def __init__(
        self,
        bus: EventBus,
        *,
        mower_id: UUID,
        account_id: UUID,
        tick: float = 0.05,
        publish_states: bool = True,
        photos: bool = True,
        **config: Any,
    ) -> None:
        self.bus = bus
        self.mower_id = mower_id
        self.account_id = account_id
        self.sim = SimulatedMower(
            mower_id=mower_id, account_id=account_id, config=SimulationConfig(**config)
        )
        self.controller = MowerController(self.sim, mower_id=mower_id, on_state=self._state)
        self.tick = tick
        self.publish_states = publish_states
        self.photos = photos
        self.commands: list[CommandEnvelope] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._subscriptions: list[Subscription] = []

    async def _state(self, state: MowerState) -> None:
        if self.publish_states:
            await self.bus.publish(telemetry_subject(self.mower_id), state)

    async def _command(self, message: BusMessage) -> None:
        envelope = message.model(CommandEnvelope)
        self.commands.append(envelope)
        result = await self.controller.handle(envelope)
        await publish_command_result(self.bus, result)

    async def _photo(self, message: BusMessage) -> None:
        request = message.model(PhotoRequest)
        if not self.photos or request.account_id != self.account_id:
            return
        photo = await self.sim.capture_photo()
        await message.respond(
            PhotoReply(
                image_base64=base64.b64encode(photo.data).decode(),
                media_type="image/jpeg",
                taken_at=utc_now(),
            )
        )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.tick)
            self.sim.tick(self.tick)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            if self.publish_states:
                await self.bus.publish(telemetry_subject(self.mower_id), self.sim.snapshot())

    async def start(self) -> None:
        await self.controller.start()
        self._subscriptions = [
            await self.bus.subscribe(command_subject(self.mower_id), self._command),
            await self.bus.subscribe(photo_subject(self.mower_id), self._photo),
        ]
        await self.bus.publish(
            device_subject(self.mower_id, "map"),
            MowerMapSnapshot(
                mower_id=self.mower_id,
                account_id=self.account_id,
                observed_at=utc_now(),
                map=await self.sim.get_map(),
            ),
        )
        await self.bus.publish(
            device_subject(self.mower_id, "capabilities"),
            MowerCapabilitiesSnapshot(
                mower_id=self.mower_id,
                account_id=self.account_id,
                observed_at=utc_now(),
                capabilities=await self.sim.capabilities(),
            ),
        )
        self._tasks = [asyncio.create_task(self._run()), asyncio.create_task(self._heartbeat())]
        await self._state(self.sim.snapshot())

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for subscription in self._subscriptions:
            await subscription.unsubscribe()
        await self.controller.close()

    def idle_at(self, x: float = 4.0, y: float = 3.0) -> None:
        """Put the mower idle on the front lawn (test shortcut into the simulator)."""
        from mowdeck_contracts.v1 import MowerActivity

        self.sim._x, self.sim._y = x, y
        self.sim._status.activity = MowerActivity.IDLE
        self.sim.tick(0)

    async def emergency_stop(self, issuer: Principal) -> None:
        """An emergency stop like Fleet publishes it."""
        await publish_command(
            self.bus,
            CommandEnvelope.create(
                mower_id=self.mower_id,
                account_id=self.account_id,
                issued_by=issuer.ref(),
                command={"kind": "emergency_stop"},  # type: ignore[arg-type]
                ttl=timedelta(seconds=10),
            ),
        )


@dataclass
class FakeFleet:
    """Answers Fleet's internal requests; issued commands are forwarded to the mower."""

    bus: EventBus
    starts: list[DriveStartRequest] = field(default_factory=list)
    stops: list[DriveStopRequest] = field(default_factory=list)
    issued: list[IssueCommandRequest] = field(default_factory=list)
    refuse: DriveErrorCode | None = None
    silent: bool = False
    refuse_issue: IssueCommandErrorCode | None = None

    async def start(self) -> None:
        await self.bus.subscribe(FLEET_DRIVE_START, self._start, queue="fleet")
        await self.bus.subscribe(FLEET_DRIVE_STOP, self._stop, queue="fleet")
        await self.bus.subscribe(FLEET_COMMANDS_ISSUE, self._issue, queue="fleet")

    async def _start(self, message: BusMessage) -> None:
        request = message.model(DriveStartRequest)
        self.starts.append(request)
        if self.silent:
            return
        if self.refuse is not None:
            await message.respond(
                DriveStartReply(error=DriveError(code=self.refuse, detail="refused by the test"))
            )
            return
        now = utc_now()
        await message.respond(
            DriveStartReply(
                started=DriveSessionStarted(
                    session_id=uuid4(),
                    mower_id=request.mower_id,
                    account_id=request.account_id,
                    started_at=now,
                    expires_at=now + timedelta(seconds=request.max_seconds),
                )
            )
        )

    async def _stop(self, message: BusMessage) -> None:
        self.stops.append(message.model(DriveStopRequest))
        await message.respond(DriveStopReply(found=True))

    async def _issue(self, message: BusMessage) -> None:
        request = message.model(IssueCommandRequest)
        self.issued.append(request)
        if self.refuse_issue is not None:
            await message.respond(
                IssueCommandReply(
                    error=IssueCommandError(code=self.refuse_issue, detail="refused by the test")
                )
            )
            return
        envelope = CommandEnvelope.create(
            mower_id=request.mower_id,
            account_id=request.account_id,
            issued_by=request.on_behalf_of,
            command=request.command.command,
            ttl=timedelta(seconds=30),
        )
        await publish_command(self.bus, envelope)
        await message.respond(
            IssueCommandReply(
                accepted=CommandAccepted(
                    command_id=envelope.command_id,
                    mower_id=envelope.mower_id,
                    kind=envelope.command.kind,
                    priority=CommandPriority.NORMAL,
                    issued_at=envelope.issued_at,
                    expires_at=envelope.expires_at,
                    correlation_id=envelope.correlation_id,
                    status_url=f"/api/v1/fleet/commands/{envelope.command_id}",
                )
            )
        )


@dataclass
class Person:
    """A principal with its access token."""

    principal: Principal
    token: str

    @property
    def headers(self) -> dict[str, str]:
        """Bearer header."""
        return bearer(self.token)

    @property
    def account_id(self) -> UUID:
        """The account of the token."""
        assert self.principal.account_id is not None
        return self.principal.account_id


@dataclass
class Harness:
    """The app under test and its fakes."""

    client: httpx.AsyncClient
    app: FastAPI
    store: MemoryStore
    bus: Any
    authz: InMemoryAuthorizer
    keys: TestKeys
    factory: StaticFactory
    entitlements: FakeEntitlements
    fleet: FakeFleet
    rigs: list[SimRig] = field(default_factory=list)

    @property
    def service(self) -> VisionService:
        """The wired use cases."""
        service: VisionService = self.app.state.vision_service
        return service

    def person(
        self, account_id: UUID | None = None, kind: PrincipalKind = PrincipalKind.USER
    ) -> Person:
        """A principal with a minted token (no tuples)."""
        principal = Principal(subject=uuid4(), account_id=account_id or uuid4(), kind=kind)
        return Person(principal, self.keys.mint(principal))

    def owner(self) -> Person:
        """A new account and its owner."""
        person = self.person()
        subject = user(person.principal.subject)
        self.authz.add(
            RelationshipTuple(subject, "owner", account(person.account_id)),
            RelationshipTuple(subject, "member", account(person.account_id)),
        )
        return person

    def viewer(self, owner: Person, mower_id: UUID) -> Person:
        """A sub-user of the owner's account who may only view one mower."""
        person = self.person(owner.account_id, PrincipalKind.SUBUSER)
        subject = user(person.principal.subject)
        self.authz.add(
            RelationshipTuple(subject, "member", account(owner.account_id)),
            RelationshipTuple(subject, "grant_mower_view", mower(mower_id)),
        )
        return person

    def mower(self, owner: Person) -> UUID:
        """A mower of the owner's account (the tuple Fleet writes)."""
        mower_id = uuid4()
        self.authz.add(
            RelationshipTuple(account(owner.account_id).as_subject(), "account", mower(mower_id))
        )
        return mower_id

    async def rig(self, owner: Person, *, docked: bool = False, **config: Any) -> SimRig:
        """A mower of the owner's account with a simulated gateway on the bus (idle on the
        front lawn unless ``docked``)."""
        mower_id = self.mower(owner)
        rig = SimRig(self.bus, mower_id=mower_id, account_id=owner.account_id, **config)
        if not docked:
            rig.idle_at()
        await rig.start()
        self.rigs.append(rig)
        await wait_for(lambda: self.service.view.state(mower_id) is not None)
        return rig

    async def proposal(self, proposal_id: str, person: Person) -> dict[str, Any]:
        """``GET /proposals/{id}``."""
        response = await self.client.get(f"{API}/proposals/{proposal_id}", headers=person.headers)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    async def until(
        self,
        proposal_id: str,
        person: Person,
        *states: ProposalState,
        timeout: float = 15.0,  # noqa: ASYNC109
    ) -> dict[str, Any]:
        """Poll the proposal until it reaches one of ``states``."""
        wanted = {s.value for s in states}
        deadline = time.monotonic() + timeout
        while True:
            body = await self.proposal(proposal_id, person)
            if body["state"] in wanted:
                return body
            if time.monotonic() > deadline:
                msg = f"proposal stayed {body['state']} ({body.get('detail')}), wanted {wanted}"
                raise AssertionError(msg)
            await asyncio.sleep(0.05)


async def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:  # noqa: ASYNC109
    """Wait until ``predicate()`` holds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            msg = "condition not reached"
            raise AssertionError(msg)
        await asyncio.sleep(0.02)


@asynccontextmanager
async def harness(
    *,
    factory: StaticFactory | None = None,
    entitlements: FakeEntitlements | None = None,
    store: MemoryStore | None = None,
    bus: EventBus | None = None,
    **overrides: Any,
) -> AsyncIterator[Harness]:
    """The app with fakes, inside its lifespan."""
    memory = store or MemoryStore()
    authz = InMemoryAuthorizer()
    bus = bus if bus is not None else InMemoryBus()
    keys = TestKeys()
    provider_factory = factory or StaticFactory()
    plans = entitlements or FakeEntitlements()
    fleet = FakeFleet(bus)
    await fleet.start()
    app = build_app(
        make_settings(**overrides),
        bus=bus,
        store=memory,
        factory=provider_factory,
        authorizer=authz,
        principal_resolver=keys.resolver(),
        entitlements=plans,
        sweep=False,
        warm_litellm=False,
    )
    async with lifespan_client(app) as client:
        h = Harness(client, app, memory, bus, authz, keys, provider_factory, plans, fleet)
        try:
            yield h
        finally:
            await h.service.close()
            for rig in h.rigs:
                await rig.stop()
