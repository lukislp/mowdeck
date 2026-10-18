"""Ports: what the vision use cases need from the outside world, as protocols."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    DriveStartReply,
    DriveStartRequest,
    DriveStopReply,
    DriveStopRequest,
    IssueCommandReply,
    IssueCommandRequest,
    MowerCapabilities,
    MowerMap,
    MowerState,
    VisionProposalCreated,
    VisionTask,
    VisionTrigger,
)

from vision.domain.context import SceneContext
from vision.domain.models import (
    Assessment,
    AutoSetting,
    Image,
    Proposal,
    ProposalState,
    ProposalStep,
    ProviderConfig,
    ProviderRequest,
    RequestKind,
    StoredImage,
    UserPreferences,
    UserProvider,
)


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


class VisionProvider(Protocol):
    """A vision model: images plus scene context in, a validated assessment out.

    Implementations: LiteLLM (any vision-capable model family) and the rule based fake. A
    native adapter (for example the Anthropic SDK) can be added behind the same port. Raises
    :class:`~vision.domain.errors.ProviderFailed` when no valid answer came back.
    """

    async def analyse(
        self, images: list[Image], context: SceneContext, task: VisionTask
    ) -> Assessment:
        """Assess the scene and propose at most six drive steps (blades off)."""
        ...


class ProviderFactory(Protocol):
    """Builds the provider for a resolved configuration (keys only live in the config)."""

    def create(self, config: ProviderConfig) -> VisionProvider:
        """A provider for ``config``."""
        ...


class SecretBox(Protocol):
    """Encrypts the users' provider secrets at rest (AES-GCM)."""

    def encrypt(self, plaintext: bytes, aad: str) -> bytes:
        """Ciphertext bound to ``aad``."""
        ...

    def decrypt(self, ciphertext: bytes, aad: str) -> bytes:
        """Inverse of :meth:`encrypt`; ``ValueError`` on a wrong key or tampering."""
        ...


class Store(Protocol):
    """Persistence of proposals, steps, images, cost accounting and user providers.

    Every read that serves an API call is filtered by the caller's account in the use case.
    """

    async def add_proposal(self, proposal: Proposal, images: Sequence[StoredImage]) -> None:
        """Insert a proposal and its images."""
        ...

    async def get_proposal(self, proposal_id: UUID) -> Proposal | None:
        """A proposal by id (any account; the caller checks)."""
        ...

    async def update_proposal(
        self,
        proposal_id: UUID,
        changes: Mapping[str, Any],
        *,
        states: Collection[ProposalState] | None = None,
    ) -> bool:
        """Apply ``changes`` (field name to value) when the state is one of ``states`` (any
        state when None); True when a row changed."""
        ...

    async def list_proposals(
        self, account_id: UUID, mower_id: UUID, *, limit: int, before: datetime | None = None
    ) -> list[Proposal]:
        """Proposals of a mower, newest first."""
        ...

    async def open_proposal(self, mower_id: UUID) -> Proposal | None:
        """The newest proposal of the mower that is not final."""
        ...

    async def chain(self, chain_id: UUID) -> list[Proposal]:
        """Every round of an assist session, oldest first."""
        ...

    async def proposals_in(
        self, states: Collection[ProposalState], *, updated_before: datetime
    ) -> list[Proposal]:
        """Proposals in ``states`` last updated before ``updated_before`` (sweeps)."""
        ...

    async def get_image(self, proposal_id: UUID, index: int) -> StoredImage | None:
        """One stored image."""
        ...

    async def purge_images(self, now: datetime) -> int:
        """Delete images past ``delete_after``; returns how many."""
        ...

    async def put_steps(self, steps: Sequence[ProposalStep]) -> None:
        """Insert the step audit rows of a proposal."""
        ...

    async def update_step(self, proposal_id: UUID, index: int, changes: Mapping[str, Any]) -> None:
        """Update one step."""
        ...

    async def steps(self, proposal_id: UUID) -> list[ProposalStep]:
        """The steps of a proposal, in order."""
        ...

    async def record_request(self, request: ProviderRequest) -> None:
        """Store the cost accounting of one model call."""
        ...

    async def count_requests(
        self,
        *,
        since: datetime,
        kind: RequestKind,
        user_id: UUID | None = None,
        account_id: UUID | None = None,
        hosted: bool | None = None,
    ) -> int:
        """Model calls since ``since`` matching the filters."""
        ...

    async def get_provider(self, user_id: UUID, account_id: UUID) -> UserProvider | None:
        """A user's own provider in an account."""
        ...

    async def put_provider(self, provider: UserProvider) -> None:
        """Insert or replace a user's provider."""
        ...

    async def delete_provider(self, user_id: UUID, account_id: UUID) -> bool:
        """Delete a user's provider; True when one existed."""
        ...

    async def delete_user(self, user_id: UUID) -> int:
        """Forget a deleted or disabled user's providers and preferences (opt-ins included);
        returns how many providers."""
        ...

    async def get_preferences(self, user_id: UUID, account_id: UUID) -> UserPreferences | None:
        """A user's AI assist preferences in an account (None: never saved)."""
        ...

    async def put_preferences(self, preferences: UserPreferences) -> None:
        """Insert or replace a user's preferences."""
        ...

    async def get_auto_setting(self, user_id: UUID, account_id: UUID) -> AutoSetting | None:
        """A user's opt-in to automatic analyses."""
        ...

    async def put_auto_setting(self, setting: AutoSetting) -> None:
        """Insert or replace a user's opt-in."""
        ...

    async def auto_subscribers(self, account_id: UUID) -> list[AutoSetting]:
        """The users of an account who opted in (enabled only), longest opted in first."""
        ...

    async def swap_auto_watch(
        self, mower_id: UUID, account_id: UUID, error_code: str | None, now: datetime
    ) -> str | None:
        """Store the mower's current recoverable error code (None: none) and return the
        previous one (None for a mower seen the first time); atomic across replicas."""
        ...

    async def count_proposals(
        self, mower_id: UUID, *, since: datetime, trigger: VisionTrigger | None = None
    ) -> int:
        """Proposals of a mower created since ``since`` (of one trigger, or any)."""
        ...


@dataclass(frozen=True, slots=True)
class ObservedState:
    """The last state of a mower and when it arrived (monotonic seconds)."""

    state: MowerState
    received: float


WatchKind = Literal["state", "emergency_stop", "result"]


@dataclass(frozen=True, slots=True)
class WatchEvent:
    """Something that happened to a mower while it is driven."""

    kind: WatchKind
    received: float
    state: MowerState | None = None
    result: CommandResult | None = None


class MowerWatch(Protocol):
    """Live events of one mower for an execution (states, emergency stops, results)."""

    async def next(self, timeout: float) -> WatchEvent | None:  # noqa: ASYNC109 - polling API
        """The next event, or None after ``timeout`` seconds."""
        ...

    def close(self) -> None:
        """Stop receiving events."""
        ...


class MowerView(Protocol):
    """What the service knows about mowers from the bus (every instance keeps its own)."""

    def state(self, mower_id: UUID) -> ObservedState | None:
        """The newest state of the mower."""
        ...

    def mower_map(self, mower_id: UUID) -> tuple[UUID, MowerMap] | None:
        """The newest map of the mower with its account id."""
        ...

    def capabilities(self, mower_id: UUID) -> tuple[UUID, MowerCapabilities] | None:
        """The newest capabilities of the mower with its account id."""
        ...

    def recent_commands(self, mower_id: UUID) -> list[tuple[str, datetime]]:
        """Recent command kinds with their issue time, oldest first (drive frames folded)."""
        ...

    def recent_track(self, mower_id: UUID) -> list[tuple[float, float, datetime]]:
        """Recent distinct positions ``(lat, lon, observed_at)`` with an RTK fix, oldest first."""
        ...

    def watch(self, mower_id: UUID) -> MowerWatch:
        """Start receiving live events of the mower."""
        ...

    def monotonic(self) -> float:
        """The monotonic clock the ``received`` stamps use."""
        ...


class PhotoSource(Protocol):
    """Still images from the mower's adapter (``gateway.mower.<id>.photo``)."""

    async def photo(self, mower_id: UUID, account_id: UUID) -> Image | None:
        """An image, or None when the gateway cannot deliver one."""
        ...


class DriveChecker(Protocol):
    """Whether a user may drive a mower (``can_drive``; OpenFGA), for automatic analyses."""

    async def can_drive(self, user_id: UUID, mower_id: UUID) -> bool:
        """True only when the user holds ``can_drive`` on the mower (fails closed)."""
        ...


class FleetGateway(Protocol):
    """Fleet's internal requests (core NATS)."""

    async def start_drive(self, request: DriveStartRequest) -> DriveStartReply:
        """``fleet.drive.start``; raises when Fleet does not answer."""
        ...

    async def stop_drive(self, request: DriveStopRequest) -> DriveStopReply | None:
        """``fleet.drive.stop``; None when Fleet does not answer (logged)."""
        ...

    async def issue(self, request: IssueCommandRequest) -> IssueCommandReply:
        """``fleet.commands.issue``; raises when Fleet does not answer."""
        ...


class FramePublisher(Protocol):
    """Publishes drive frames on ``command.mower.<id>``."""

    async def frame(self, envelope: CommandEnvelope) -> None:
        """Publish one frame; raises when the bus does not take it."""
        ...


class EventPublisher(Protocol):
    """Publishes the service's events."""

    async def proposal_created(self, event: VisionProposalCreated) -> None:
        """``vision.proposal.created`` (failures are logged, never raised)."""
        ...
