"""Driving an approved proposal: a drive session on behalf of the approver, frames at a fixed
rate with the dead-man ``ttl_ms`` and blades off, a safety watch, then the verification.

Sequence (ADR 0033):

1. Check the start: a recent state (``start_state_max_age``), no sensor stop, a known map
   (the geofence needs it) and a position inside the geofence.
2. ``fleet.drive.start`` on behalf of the approving user (``origin = vision``): Fleet checks
   ``can_drive`` for that user again and refuses a second session of the mower.
3. Per step ``round(seconds * rate)`` frames, one every ``1 / rate`` seconds, each a
   ``ManualDrive`` with ``blade_on = False`` and ``ttl_ms = 1000`` that expires with its
   dead-man time. Between frames every event is applied: an emergency stop, a sensor stop, a new
   error, another activity, a refused frame, a geofence exit, no state for ``state_timeout``
   seconds or a cancellation stops the run at once.
4. The end publishes a zero frame while the last frame still lives (not after an emergency or
   sensor stop: the controller already ended the session, a frame would only reopen one) and
   closes the record with ``fleet.drive.stop``. A cancelled task (shutdown) does the same.
5. After ``settle_seconds`` the outcome is checked; for ``find_dock`` a successful approach
   ends with a normal ``return_to_dock`` through ``fleet.commands.issue``.

A proposal with action ``return_to_dock`` drives nothing itself: with a recent state the route
check (:func:`~vision.domain.dock.dock_route`: no lift, tilt or safety stop, the mower in a
mapped area linked to the dock) runs again on the newest state and map, then the normal
``return_to_dock`` goes through ``fleet.commands.issue`` on behalf of the approver; the mower
drives home by itself.

A proposal with action ``resume`` continues the paused mowing job, in this order:

1. The job context (:func:`~vision.domain.job.job_context`) is built again from the newest
   state, route and map; without a paused job that ``resume_mow`` can continue (no job, a
   running job, a spot run, a lift, tilt or safety stop) nothing runs (``aborted``).
2. Its drive steps, if any, run exactly like a ``drive`` (steps 1 to 5 above, blades off, the
   same limits and aborts); a run that aborts or does not free the mower ends there
   (``aborted`` or ``not_recovered``, "the job was not resumed").
3. Verify what the drive can establish (:func:`~vision.domain.rules.resume_gate`): a recent
   state, not lifted, not offline, no sensor stop and no new error (a lift, tilt, collision,
   blade or motor fault, or another code), and the job still paused or paused by the error.
   The error that stopped the job may still be reported: a LUBA clears a stuck error only after
   the resumed job moved it a few metres, never after a manual drive. Otherwise
   ``not_recovered``.
4. ``resume_mow`` (the command of the app's "continue") through ``fleet.commands.issue`` on
   behalf of the approver, sent once and never repeated automatically. Then up to
   ``resume_confirm_seconds`` (default 60 s) for any of: the activity becomes ``mowing``, the
   error clears, or the mower moved at least ``resume_confirm_move_m`` (default 2 m) from where
   ``resume_mow`` was sent: ``recovered``. A refused or failed command, an emergency stop, a
   lift or a new error is ``not_recovered`` at once; still in the same error at the same place
   after the window is ``not_recovered`` with that reason.
"""

import asyncio
import contextlib
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandRequest,
    CommandStatus,
    DriveEndReason,
    DriveStartRequest,
    DriveStopRequest,
    IssueCommandRequest,
    ManualDrive,
    MowerActivity,
    MowerMap,
    MowerState,
    PrincipalRef,
    ResumeMow,
    ReturnToDock,
    VisionTask,
    drive_correlation_id,
)
from mowdeck_service_kit import get_logger

from vision.application.ports import (
    Clock,
    FleetGateway,
    FramePublisher,
    MowerView,
    MowerWatch,
    Store,
    WatchEvent,
)
from vision.domain.context import Pose, pose_of
from vision.domain.dock import dock_route
from vision.domain.job import JobContext, job_context, resume_problem
from vision.domain.models import (
    Proposal,
    ProposalAction,
    ProposalState,
    ProposedStep,
    StepOutcome,
)
from vision.domain.rules import (
    FRAME_TTL_MS,
    Geofence,
    frames_for,
    resume_gate,
    start_problem,
    state_abort_reason,
    total_seconds,
    verify,
)

_log = get_logger("vision.executor")

_NO_STOP_FRAME = ("emergency_stop", "sensor_stop")
_FLEET_SILENT = "Fleet did not answer"
_RESUMABLE = frozenset({"paused", "paused_by_error"})


@dataclass(frozen=True, slots=True)
class DriveConfig:
    """Timing and limits of the execution."""

    rate_hz: float = 5.0
    state_timeout_seconds: float = 3.0
    start_state_max_age_seconds: float = 30.0
    """A resting mower reports only every heartbeat (10 s on the Device-Gateway); the start
    accepts a state this old, the drive session then makes the gateway report every second."""
    geofence_margin_m: float = 2.0
    settle_seconds: float = 1.5
    min_move_m: float = 0.2
    resume_confirm_seconds: float = 60.0
    """After ``resume_mow``: how long to wait for mowing, the error to clear or the move."""
    resume_confirm_move_m: float = 2.0
    """After ``resume_mow``: this far from where it was sent counts as moving on its job."""


@dataclass(slots=True)
class RunOutcome:
    """How an execution ended."""

    state: ProposalState
    detail: str
    session_id: UUID | None = None
    start_pose: Pose | None = None
    end_pose: Pose | None = None
    return_command_id: UUID | None = None
    """The command sent after approval: ``return_to_dock`` or ``resume_mow``."""


@dataclass(slots=True)
class _Run:
    proposal: Proposal
    approver: PrincipalRef
    account_id: UUID
    watch: MowerWatch
    mower_map: MowerMap
    fence: Geofence
    initial: MowerState
    last_state: MowerState
    last_received: float
    session_id: UUID | None = None
    travelled_m: float = 0.0
    last_pose: Pose | None = None
    frames: int = 0
    last_frame_at: float | None = None
    closed: bool = False
    pending: list[WatchEvent] = field(default_factory=list)

    @property
    def correlation_id(self) -> str | None:
        return drive_correlation_id(self.session_id) if self.session_id else None


Cancelled = Callable[[], Awaitable[bool]]


class Executor:
    """Drives approved proposals (one run per proposal)."""

    def __init__(
        self,
        *,
        store: Store,
        fleet: FleetGateway,
        frames: FramePublisher,
        view: MowerView,
        clock: Clock,
        config: DriveConfig,
    ) -> None:
        self.store = store
        self.fleet = fleet
        self.frames = frames
        self.view = view
        self.clock = clock
        self.config = config

    async def run(
        self,
        proposal: Proposal,
        approver: PrincipalRef,
        account_id: UUID,
        *,
        cancelled: Cancelled,
        on_verifying: Callable[[UUID | None], Awaitable[None]],
    ) -> RunOutcome:
        """Execute the approved proposal (steps, dock command or resume) and check the outcome."""
        assert proposal.assessment is not None  # noqa: S101 - only proposed rows get here
        if proposal.action is ProposalAction.RETURN_TO_DOCK:
            return await self._dock_return(proposal, approver, account_id)
        if proposal.action is ProposalAction.RESUME:
            return await self._resume(
                proposal, approver, account_id, cancelled=cancelled, on_verifying=on_verifying
            )
        return await self._drive_steps(
            proposal,
            approver,
            account_id,
            proposal.assessment.proposal,
            cancelled=cancelled,
            on_verifying=on_verifying,
        )

    async def _drive_steps(
        self,
        proposal: Proposal,
        approver: PrincipalRef,
        account_id: UUID,
        steps: list[ProposedStep],
        *,
        cancelled: Cancelled,
        on_verifying: Callable[[UUID | None], Awaitable[None]],
    ) -> RunOutcome:
        """Drive ``steps`` in a drive session and verify that they helped."""
        watch = self.view.watch(proposal.mower_id)
        try:
            prepared = self._prepare(proposal, approver, account_id, watch)
            if isinstance(prepared, RunOutcome):
                return prepared
            run = prepared
            start_pose = pose_of(run.initial, run.mower_map)
            reply = await self._start_session(run, steps)
            if isinstance(reply, RunOutcome):
                reply.start_pose = start_pose
                return reply
            try:
                reason = await self._drive(run, steps, cancelled)
            except asyncio.CancelledError:
                await asyncio.shield(self._finish(run, "cancelled: the service stopped"))
                raise
            except Exception as exc:
                _log.exception("vision run failed", proposal_id=str(proposal.id))
                await self._finish(run, f"failed: {type(exc).__name__}")
                return RunOutcome(
                    ProposalState.FAILED,
                    f"execution failed: {type(exc).__name__}",
                    run.session_id,
                    start_pose,
                )
            await self._finish(run, reason)
            if reason is not None:
                end = pose_of(run.last_state, run.mower_map)
                return RunOutcome(ProposalState.ABORTED, reason, run.session_id, start_pose, end)
            await on_verifying(run.session_id)
            return await self._verify(run, steps, start_pose)
        finally:
            watch.close()

    # --- return to dock -------------------------------------------------------------------

    async def _dock_return(
        self, proposal: Proposal, approver: PrincipalRef, account_id: UUID
    ) -> RunOutcome:
        """Check the route on the newest state and map again, then the normal dock command."""
        observed = self.view.state(proposal.mower_id)
        now = self.view.monotonic()
        if observed is None or now - observed.received > self.config.start_state_max_age_seconds:
            return RunOutcome(ProposalState.ABORTED, "no_state: no recent state of the mower")
        if observed.state.account_id != proposal.account_id:
            return RunOutcome(ProposalState.ABORTED, "no_state: no state of this account")
        mapped = self.view.mower_map(proposal.mower_id)
        mower_map = mapped[1] if mapped is not None and mapped[0] == proposal.account_id else None
        pose = pose_of(observed.state, mower_map) if mower_map is not None else None
        route = dock_route(mower_map, (pose.x, pose.y) if pose else None, observed.state)
        if not route.return_to_dock_applicable:
            detail = f"return to dock is not applicable any more: {route.reason}"
            return RunOutcome(ProposalState.ABORTED, detail[:500], start_pose=pose)
        command_id, problem = await self._issue_dock(approver, account_id, proposal.mower_id)
        if problem is not None:
            failed = problem.startswith(_FLEET_SILENT)
            state = ProposalState.FAILED if failed else ProposalState.NOT_RECOVERED
            return RunOutcome(state, problem, start_pose=pose)
        _log.info("vision return to dock issued", proposal_id=str(proposal.id))
        return RunOutcome(
            ProposalState.RECOVERED,
            f"returning to the dock ({route.reason})"[:500],
            start_pose=pose,
            return_command_id=command_id,
        )

    async def _issue_dock(
        self, approver: PrincipalRef, account_id: UUID, mower_id: UUID
    ) -> tuple[UUID | None, str | None]:
        """``return_to_dock`` through Fleet: the command id, or the problem."""
        return await self._issue(approver, account_id, mower_id, ReturnToDock())

    async def _issue(
        self,
        approver: PrincipalRef,
        account_id: UUID,
        mower_id: UUID,
        command: ReturnToDock | ResumeMow,
    ) -> tuple[UUID | None, str | None]:
        """A job command through Fleet on the approver's behalf: the command id, or the
        problem (starting with :data:`_FLEET_SILENT` when Fleet did not answer)."""
        request = IssueCommandRequest(
            on_behalf_of=approver,
            account_id=account_id,
            mower_id=mower_id,
            command=CommandRequest(command=command),
            origin="vision",
        )
        try:
            reply = await self.fleet.issue(request)
        except Exception as exc:
            _log.warning("vision command failed", kind=command.kind, error=repr(exc))
            return None, f"{_FLEET_SILENT} {command.kind}"
        if reply.accepted is None:
            code = reply.error.code if reply.error else "unknown"
            return None, f"{command.kind} was refused: {code}"
        return reply.accepted.command_id, None

    # --- resume ---------------------------------------------------------------------------

    def _recent_state(self, proposal: Proposal) -> tuple[MowerState | None, str | None]:
        """The newest state of the proposal's mower, or why there is none to act on."""
        observed = self.view.state(proposal.mower_id)
        now = self.view.monotonic()
        if observed is None or now - observed.received > self.config.start_state_max_age_seconds:
            return None, "no_state: no recent state of the mower"
        if observed.state.account_id != proposal.account_id:
            return None, "no_state: no state of this account"
        return observed.state, None

    def _job(self, proposal: Proposal, state: MowerState) -> JobContext | None:
        """The job context on the newest state, route and map (same account only)."""
        mapped = self.view.mower_map(proposal.mower_id)
        mower_map = mapped[1] if mapped is not None and mapped[0] == proposal.account_id else None
        routed = self.view.mow_path(proposal.mower_id)
        path = routed[1] if routed is not None and routed[0] == proposal.account_id else None
        pose = pose_of(state, mower_map)
        return job_context(
            state,
            mower_map=mower_map,
            here=(pose.x, pose.y) if pose else None,
            theta_rad=pose.theta_rad if pose else None,
            path=path,
            last_start=self.view.last_start(proposal.mower_id),
        )

    async def _resume(
        self,
        proposal: Proposal,
        approver: PrincipalRef,
        account_id: UUID,
        *,
        cancelled: Cancelled,
        on_verifying: Callable[[UUID | None], Awaitable[None]],
    ) -> RunOutcome:
        """Check the job, drive the optional steps, verify, then ``resume_mow``."""
        assert proposal.assessment is not None  # noqa: S101 - only proposed rows get here
        state, missing = self._recent_state(proposal)
        if state is None:
            return RunOutcome(ProposalState.ABORTED, missing or "no_state")
        refused = resume_problem(self._job(proposal, state), state)
        if refused is not None:
            return RunOutcome(ProposalState.ABORTED, f"resume refused: {refused}"[:500])
        steps = proposal.assessment.proposal
        if not steps:
            await on_verifying(None)
            nothing_driven = RunOutcome(ProposalState.RECOVERED, "")
            return await self._send_resume(proposal, approver, account_id, nothing_driven, state)
        driven = await self._drive_steps(
            proposal, approver, account_id, steps, cancelled=cancelled, on_verifying=on_verifying
        )
        if driven.state is not ProposalState.RECOVERED:
            driven.detail = f"{driven.detail}; the job was not resumed"[:500]
            return driven
        return await self._send_resume(proposal, approver, account_id, driven, state)

    async def _send_resume(
        self,
        proposal: Proposal,
        approver: PrincipalRef,
        account_id: UUID,
        outcome: RunOutcome,
        initial: MowerState,
    ) -> RunOutcome:
        """Check what the drive could establish (not that the error cleared), send
        ``resume_mow`` once and confirm that the job continues; completes ``outcome`` (of the
        drive, if any). ``initial`` is the state when the execution started."""
        before = f"{outcome.detail}; " if outcome.detail else ""
        state, missing = self._recent_state(proposal)
        problem = missing or resume_gate(state, initial)
        job = self._job(proposal, state) if state is not None else None
        if problem is None and state is not None:
            problem = resume_problem(job, state)
            if problem is None and job is not None and job.status not in _RESUMABLE:
                problem = f"the job is {job.status.replace('_', ' ')}"
        if problem is not None or state is None:
            outcome.state = ProposalState.NOT_RECOVERED
            outcome.detail = f"{before}the job was not resumed: {problem}"[:500]
            return outcome
        watch = self.view.watch(proposal.mower_id)
        try:
            command_id, issue_problem = await self._issue(
                approver, account_id, proposal.mower_id, ResumeMow()
            )
            if issue_problem is not None or command_id is None:
                issue_problem = issue_problem or "resume_mow was not accepted"
                failed = issue_problem.startswith(_FLEET_SILENT)
                outcome.state = ProposalState.FAILED if failed else ProposalState.NOT_RECOVERED
                outcome.detail = f"{before}{issue_problem}"[:500]
                return outcome
            outcome.return_command_id = command_id
            _log.info("vision resume issued", proposal_id=str(proposal.id))
            continued, why = await self._await_continue(proposal, watch, command_id, state)
        finally:
            watch.close()
        if continued:
            outcome.state = ProposalState.RECOVERED
            outcome.detail = f"{before}the job continues{_job_words(job)}: {why}"[:500]
        else:
            outcome.state = ProposalState.NOT_RECOVERED
            detail = f"{before}resume_mow was sent once, the job did not continue: {why}"
            outcome.detail = detail[:500]
        return outcome

    async def _await_continue(
        self, proposal: Proposal, watch: MowerWatch, command_id: UUID, sent: MowerState
    ) -> tuple[bool, str]:
        """Whether the job continues after ``resume_mow``, with the reason: the activity becomes
        ``mowing``, the error clears, or the mower moves ``resume_confirm_move_m`` from where
        the command was sent (``sent``), within ``resume_confirm_seconds``. A refusal, an
        emergency stop, a lift or a new error ends the wait at once."""
        mapped = self.view.mower_map(proposal.mower_id)
        mower_map = mapped[1] if mapped is not None and mapped[0] == proposal.account_id else None
        origin = pose_of(sent, mower_map)
        last = sent
        moved = 0.0
        window = self.config.resume_confirm_seconds
        deadline = self.view.monotonic() + window
        while (remaining := deadline - self.view.monotonic()) > 0:
            event = await watch.next(remaining)
            if event is None:
                break
            if event.kind == "emergency_stop":
                return False, "an emergency stop came"
            if event.kind == "result" and event.result is not None:
                result = event.result
                if result.command_id == command_id and result.status in (
                    CommandStatus.REJECTED,
                    CommandStatus.FAILED,
                ):
                    return False, f"the mower refused it: {result.reason or result.status}"
                continue
            state = event.state
            if event.kind != "state" or state is None or state.account_id != proposal.account_id:
                continue
            last = state
            if state.activity is MowerActivity.MOWING:
                return True, "mowing again"
            stop = resume_gate(state, sent)
            if stop is not None:
                return False, stop
            if state.activity is not MowerActivity.ERROR:
                return True, f"the error cleared, the mower is {state.activity}"
            here = pose_of(state, mower_map)
            if origin is not None and here is not None:
                moved = max(moved, math.hypot(here.x - origin.x, here.y - origin.y))
                if moved >= self.config.resume_confirm_move_m:
                    return True, f"moved {moved:.1f} m on its job"
        code = last.error_code or str(last.activity)
        return False, (
            f"after {window:g} s the mower still reports {code}, {moved:.1f} m from where "
            "resume_mow was sent; it is not sent again"
        )

    # --- start ----------------------------------------------------------------------------

    def _prepare(
        self, proposal: Proposal, approver: PrincipalRef, account_id: UUID, watch: MowerWatch
    ) -> _Run | RunOutcome:
        observed = self.view.state(proposal.mower_id)
        now = self.view.monotonic()
        if observed is None or now - observed.received > self.config.start_state_max_age_seconds:
            return RunOutcome(ProposalState.ABORTED, "no_state: no recent state of the mower")
        if observed.state.account_id != proposal.account_id:
            return RunOutcome(ProposalState.ABORTED, "no_state: no state of this account")
        problem = start_problem(proposal.task, observed.state)
        if problem is not None:
            return RunOutcome(ProposalState.ABORTED, problem)
        mapped = self.view.mower_map(proposal.mower_id)
        if mapped is None or mapped[0] != proposal.account_id:
            return RunOutcome(
                ProposalState.ABORTED, "no map of the mower: the geofence cannot be checked"
            )
        mower_map = mapped[1]
        fence = Geofence(mower_map, self.config.geofence_margin_m)
        pose = pose_of(observed.state, mower_map)
        if pose is None:
            return RunOutcome(ProposalState.ABORTED, "the position of the mower is unknown")
        violation = fence.violation((pose.x, pose.y))
        if violation is not None:
            return RunOutcome(ProposalState.ABORTED, violation)
        return _Run(
            proposal=proposal,
            approver=approver,
            account_id=account_id,
            watch=watch,
            mower_map=mower_map,
            fence=fence,
            initial=observed.state,
            last_state=observed.state,
            last_received=observed.received,
            last_pose=pose,
        )

    async def _start_session(self, run: _Run, steps: list[ProposedStep]) -> RunOutcome | None:
        request = DriveStartRequest(
            on_behalf_of=run.approver,
            account_id=run.account_id,
            mower_id=run.proposal.mower_id,
            blade_on_allowed=False,
            max_seconds=min(3600, math.ceil(total_seconds(steps)) + 30),
            origin="vision",
        )
        try:
            reply = await self.fleet.start_drive(request)
        except Exception as exc:
            _log.warning("fleet.drive.start failed", error=repr(exc))
            return RunOutcome(ProposalState.FAILED, "Fleet did not answer the drive request")
        if reply.error is not None:
            detail = f"drive refused: {reply.error.code}: {reply.error.detail}"
            return RunOutcome(ProposalState.ABORTED, detail[:500])
        assert reply.started is not None  # noqa: S101 - exactly one of both
        run.session_id = reply.started.session_id
        # The liveness watch starts now: the open session makes the gateway report every second.
        run.last_received = self.view.monotonic()
        _log.info(
            "vision drive session started",
            proposal_id=str(run.proposal.id),
            session_id=str(run.session_id),
        )
        return None

    # --- driving --------------------------------------------------------------------------

    async def _drive(
        self, run: _Run, steps: list[ProposedStep], cancelled: Cancelled
    ) -> str | None:
        interval = 1.0 / self.config.rate_hz
        pid = run.proposal.id
        for index, step in enumerate(steps):
            await self.store.update_step(
                pid, index, {"started_at": self.clock(), "outcome": StepOutcome.PENDING}
            )
            sent = 0
            reason: str | None = None
            for _ in range(frames_for(step, self.config.rate_hz)):
                reason = self._stale(run) or (
                    "cancelled: stopped by a user" if await cancelled() else None
                )
                if reason is None:
                    await self._frame(run, step.linear, step.angular)
                    sent += 1
                    reason = await self._wait(run, interval)
                if reason is not None:
                    break
            if reason is not None:
                await self.store.update_step(
                    pid,
                    index,
                    {
                        "outcome": StepOutcome.ABORTED,
                        "ended_at": self.clock(),
                        "frames": sent,
                        "detail": reason[:500],
                    },
                )
                for later in range(index + 1, len(steps)):
                    await self.store.update_step(pid, later, {"outcome": StepOutcome.SKIPPED})
                return reason
            await self.store.update_step(
                pid, index, {"outcome": StepOutcome.DONE, "ended_at": self.clock(), "frames": sent}
            )
        return None

    def _stale(self, run: _Run) -> str | None:
        age = self.view.monotonic() - run.last_received
        if age > self.config.state_timeout_seconds:
            return f"no_state: no state for {age:.1f} s"
        return None

    async def _wait(self, run: _Run, seconds: float) -> str | None:
        deadline = self.view.monotonic() + seconds
        while True:
            remaining = deadline - self.view.monotonic()
            if remaining <= 0:
                return None
            event = await run.watch.next(remaining)
            if event is None:
                return None
            reason = self._apply(run, event)
            if reason is not None:
                return reason

    def _apply(self, run: _Run, event: WatchEvent) -> str | None:
        if event.kind == "emergency_stop":
            return "emergency_stop: someone stopped the mower"
        if event.kind == "result" and event.result is not None:
            result = event.result
            if result.correlation_id == run.correlation_id and result.status in (
                CommandStatus.REJECTED,
                CommandStatus.FAILED,
            ):
                return f"mower_refused: {result.reason or result.status}"[:500]
            return None
        if event.kind == "state" and event.state is not None:
            state = event.state
            if state.account_id != run.proposal.account_id:
                return None
            run.last_state = state
            run.last_received = event.received
            reason = state_abort_reason(state, run.initial)
            if reason is not None:
                return reason
            pose = pose_of(state, run.mower_map)
            if pose is not None:
                self._track(run, pose)
                return run.fence.violation((pose.x, pose.y))
        return None

    @staticmethod
    def _track(run: _Run, pose: Pose) -> None:
        """Add the path since the last position (jitter below 1 cm ignored)."""
        if run.last_pose is not None:
            step = math.hypot(pose.x - run.last_pose.x, pose.y - run.last_pose.y)
            if step < 0.01:
                return
            run.travelled_m += step
        run.last_pose = pose

    async def _frame(self, run: _Run, linear: float, angular: float) -> None:
        assert run.session_id is not None  # noqa: S101 - set before driving
        envelope = CommandEnvelope.create(
            mower_id=run.proposal.mower_id,
            account_id=run.account_id,
            issued_by=run.approver,
            command=ManualDrive(
                linear=linear, angular=angular, blade_on=False, ttl_ms=FRAME_TTL_MS
            ),
            ttl=timedelta(milliseconds=FRAME_TTL_MS),
            correlation_id=drive_correlation_id(run.session_id),
            now=self.clock(),
        )
        await self.frames.frame(envelope)
        run.frames += 1
        run.last_frame_at = self.view.monotonic()

    async def _finish(self, run: _Run, reason: str | None) -> None:
        """Zero frame (when it helps) and ``fleet.drive.stop``; idempotent."""
        if run.closed or run.session_id is None:
            return
        run.closed = True
        frame_alive = (
            run.last_frame_at is not None
            and self.view.monotonic() - run.last_frame_at < FRAME_TTL_MS / 1000
        )
        if frame_alive and not (reason or "").startswith(_NO_STOP_FRAME):
            with contextlib.suppress(Exception):
                await self._frame(run, 0.0, 0.0)
        end = DriveEndReason.COMPLETED if reason is None else DriveEndReason.ABORTED
        with contextlib.suppress(Exception):
            await self.fleet.stop_drive(
                DriveStopRequest(
                    session_id=run.session_id,
                    account_id=run.account_id,
                    mower_id=run.proposal.mower_id,
                    reason=end,
                    ended_at=self.clock(),
                    frames=run.frames,
                    frames_dropped=0,
                )
            )
        _log.info(
            "vision drive session ended",
            proposal_id=str(run.proposal.id),
            session_id=str(run.session_id),
            reason=reason or "completed",
            frames=run.frames,
        )

    # --- verification ---------------------------------------------------------------------

    async def _verify(
        self, run: _Run, steps: list[ProposedStep], start_pose: Pose | None
    ) -> RunOutcome:
        deadline = self.view.monotonic() + self.config.settle_seconds
        while (remaining := deadline - self.view.monotonic()) > 0:
            event = await run.watch.next(remaining)
            if event is None:
                break
            if (
                event.kind == "state"
                and event.state is not None
                and event.state.account_id == run.proposal.account_id
            ):
                run.last_state = event.state
                run.last_received = event.received
                settled = pose_of(event.state, run.mower_map)
                if settled is not None:
                    self._track(run, settled)
        end_pose = pose_of(run.last_state, run.mower_map)
        ok, detail = verify(
            run.proposal.task,
            start=start_pose,
            end=end_pose,
            end_state=run.last_state,
            steps=steps,
            min_move_m=self.config.min_move_m,
            travelled_m=run.travelled_m,
            initial=run.initial,
        )
        outcome = RunOutcome(
            ProposalState.RECOVERED if ok else ProposalState.NOT_RECOVERED,
            detail,
            run.session_id,
            start_pose,
            end_pose,
        )
        if ok and run.proposal.task is VisionTask.FIND_DOCK:
            await self._return_to_dock(run, outcome)
        return outcome

    async def _return_to_dock(self, run: _Run, outcome: RunOutcome) -> None:
        command_id, problem = await self._issue_dock(
            run.approver, run.account_id, run.proposal.mower_id
        )
        if problem is not None:
            outcome.state = ProposalState.NOT_RECOVERED
            outcome.detail = f"approached the dock, but {problem}"
            return
        outcome.return_command_id = command_id


def _job_words(job: JobContext | None) -> str:
    """The job's areas for an outcome text: ``(Front lawn, next Back lawn)``, or empty."""
    if job is None or job.current_area is None:
        return ""
    if job.next_area is None:
        return f" ({job.current_area})"
    return f" ({job.current_area}, next {job.next_area})"
