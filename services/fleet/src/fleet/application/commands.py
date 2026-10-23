"""Commands: send to one mower or to every mower of a site or group, follow the results, keep
the record (the command audit) for the retention period.

The HTTP layer has already checked the relation the command kind needs on the target
(``relation_for``) before anything here runs, so a refused command touches neither the
database nor the bus. Envelopes are always built from the verified principal and the mower
row: ``issued_by`` is the caller, ``account_id`` is the mower's account, never a body value.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from mowdeck_authz import account, mower
from mowdeck_contracts.v1 import (
    BulkCommandAccepted,
    BulkCommandItem,
    BulkCommandOutcome,
    CommandAccepted,
    CommandEnvelope,
    CommandPriority,
    CommandRequest,
    CommandResult,
    CommandState,
    CommandView,
    ContractModel,
    IssueCommandError,
    IssueCommandErrorCode,
    IssueCommandReply,
    IssueCommandRequest,
    Principal,
    PrincipalKind,
    PrincipalRef,
    SpotMow,
    SpotMowPlan,
    StartMow,
)
from mowdeck_service_kit.correlation import get_correlation_id
from mowdeck_service_kit.entitlements import Entitlements

from fleet.application.access import Access
from fleet.application.context import Actor, FleetConfig
from fleet.application.plan import (
    EMERGENCY_STOP,
    FEATURE_BULK,
    FEATURE_SPOT,
    allow_all,
    require_feature,
)
from fleet.application.ports import Clock, CommandPublisher, In, Lt, Store
from fleet.domain.errors import (
    BaseStationNotLinkedError,
    FeatureNotInPlanError,
    ForbiddenError,
    InvalidInput,
    NotFoundError,
    Unavailable,
)
from fleet.domain.models import CommandRecord, Mower
from fleet.domain.rules import (
    CAN_MANAGE_MOWERS,
    CAN_VIEW,
    SINGLE_MOWER_KINDS,
    advances_from,
    command_features,
    command_ttl,
    relation_for,
)

_log = logging.getLogger("fleet.commands")

RETENTION_LOCK = 0x466C6565_74436D64  # "FleetCmd": one replica purges at a time
_CORRELATION = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
NOT_PUBLISHED = "not published: the event bus is not reachable"
BASE_STATION_UPDATE = "base_station_firmware_update"
"""Command kind that needs ``can_manage_mowers`` on the account and a linked base station."""


def _refused(code: IssueCommandErrorCode, detail: str) -> IssueCommandReply:
    return IssueCommandReply(error=IssueCommandError(code=code, detail=detail[:500]))


@dataclass(frozen=True, slots=True)
class CommandPage:
    """A page of command records, newest first; ``next_before`` continues it."""

    items: list[CommandView]
    next_before: datetime | None


def view(record: CommandRecord) -> CommandView:
    """The client's view of a record."""
    return CommandView(
        command_id=record.command_id,
        mower_id=record.mower_id,
        bulk_id=record.bulk_id,
        kind=record.kind,
        priority=CommandPriority(record.priority),
        status=CommandState(record.status),
        reason=record.reason,
        issued_by=PrincipalRef.model_validate(record.issued_by),
        issued_at=record.issued_at,
        expires_at=record.expires_at,
        result_at=record.result_at,
        correlation_id=record.correlation_id,
        origin=record.origin,
        schedule_id=record.schedule_id,
        plan_hash=record.plan_hash,
        end_job=bool(record.command.get("end_job", False))
        if record.kind == "return_to_dock"
        else None,
        pattern=_start_pattern(record),
    )


def _start_pattern(record: CommandRecord) -> dict[str, Any] | None:
    """The pattern settings of a ``start_mow`` on map areas (the history shows them)."""
    if record.kind != "start_mow" or record.command.get("spot_plan") is not None:
        return None
    pattern = record.command.get("pattern")
    return dict(pattern) if isinstance(pattern, dict) else None


def spot_plan_of(command: ContractModel) -> tuple[str, SpotMowPlan] | None:
    """The Planner route a command carries and the field it sits in: ``start_mow.spot_plan``
    (a native job of the mower) or ``spot_mow.plan`` (a run MowDeck drives, ADR 0039)."""
    if isinstance(command, StartMow) and command.spot_plan is not None:
        return "spot_plan", command.spot_plan
    if isinstance(command, SpotMow):
        return "plan", command.plan
    return None


def audit_form(command: ContractModel) -> tuple[dict[str, Any], str | None]:
    """The command as stored in the record, and the spot plan hash if any.

    A spot plan is kept as a summary (hash, chain, sizes, estimates): the route itself travels
    with the command and can be computed again by the Planner, the audit needs its identity.
    """
    dumped: dict[str, Any] = command.model_dump(mode="json")
    carried = spot_plan_of(command)
    if carried is not None:
        field_name, plan = carried
        dumped[field_name] = {
            "plan_hash": plan.plan_hash,
            "pattern_chain": list(plan.pattern_chain),
            "segments": len(plan.segments),
            "estimated_duration_s": plan.estimated_duration_s,
            "covered_area_m2": plan.covered_area_m2,
            "native": plan.native.model_dump(mode="json") if plan.native else None,
        }
        return dumped, plan.plan_hash
    return dumped, None


class CommandService:
    """Use cases of commands."""

    def __init__(
        self,
        store: Store,
        access: Access,
        publisher: CommandPublisher,
        config: FleetConfig,
        clock: Clock,
        entitlements: Entitlements | None = None,
    ) -> None:
        self.store = store
        self.access = access
        self.publisher = publisher
        self.config = config
        self.clock = clock
        self.entitlements = entitlements if entitlements is not None else allow_all()

    # --- building ------------------------------------------------------------------------

    def _envelope(
        self, actor: Actor, target: Mower, request: CommandRequest, now: datetime
    ) -> CommandEnvelope:
        if actor.principal is None:  # pragma: no cover - routes always pass a principal
            msg = "commands need a principal"
            raise InvalidInput(msg)
        kind = request.command.kind
        correlation = actor.correlation_id
        return CommandEnvelope.create(
            mower_id=target.id,
            account_id=target.account_id,
            issued_by=actor.principal.ref(),
            command=request.command,
            ttl=command_ttl(kind, request.ttl_seconds, self.config.limits),
            correlation_id=correlation if correlation and _CORRELATION.match(correlation) else None,
            now=now,
        )

    @staticmethod
    def _record(
        envelope: CommandEnvelope,
        bulk_id: UUID | None = None,
        *,
        origin: str | None = None,
        schedule_id: UUID | None = None,
    ) -> CommandRecord:
        command, plan_hash = audit_form(envelope.command)
        return CommandRecord(
            command_id=envelope.command_id,
            account_id=envelope.account_id,
            mower_id=envelope.mower_id,
            kind=envelope.command.kind,
            command=command,
            plan_hash=plan_hash,
            priority=envelope.priority.value,
            issued_by_subject=envelope.issued_by.subject,
            issued_by=envelope.issued_by.model_dump(mode="json"),
            issued_at=envelope.issued_at,
            expires_at=envelope.expires_at,
            status=CommandState.PENDING.value,
            correlation_id=envelope.correlation_id,
            bulk_id=bulk_id,
            origin=origin,
            schedule_id=schedule_id,
        )

    async def _publish(self, envelope: CommandEnvelope) -> bool:
        """Publish; on failure the record becomes ``failed`` (a late start is worse than none,
        so there is no retry)."""
        try:
            await self.publisher.publish(envelope)
        except Exception as exc:
            _log.warning(
                "publishing a command failed",
                extra={"command_id": str(envelope.command_id), "error": type(exc).__name__},
            )
            async with self.store.transaction() as tx:
                await tx.commands.update_where(
                    {
                        "status": CommandState.FAILED.value,
                        "reason": NOT_PUBLISHED,
                        "result_at": self.clock(),
                    },
                    command_id=envelope.command_id,
                )
            return False
        return True

    async def _check_features(self, account_id: UUID, request: CommandRequest) -> None:
        """The plan features of the kind (``command_features``), plus ``spot_mowing`` for a
        ``start_mow`` with a spot plan; a carried spot plan must fit the size limit."""
        command = request.command
        features = set(command_features(command.kind))
        carried = spot_plan_of(command)
        if carried is not None:
            _, plan = carried
            size = len(plan.model_dump_json())
            if size > self.config.spot_plan_max_bytes:
                msg = (
                    f"the spot plan has {size} bytes as JSON, at most "
                    f"{self.config.spot_plan_max_bytes} are accepted"
                )
                raise InvalidInput(msg)
            features.add(FEATURE_SPOT)
        for feature in sorted(features):
            await require_feature(self.entitlements, account_id, feature)

    # --- sending -------------------------------------------------------------------------

    async def send(
        self,
        actor: Actor,
        account_id: UUID,
        mower_id: UUID,
        request: CommandRequest,
        *,
        origin: str | None = None,
        schedule_id: UUID | None = None,
    ) -> CommandAccepted:
        """Record and publish a command for one mower (the caller holds the relation).

        Every kind except the emergency stop needs the plan feature ``control``; ``set_light``
        also ``camera``, ``spot_mow`` (and a ``start_mow`` with a spot plan) also
        ``spot_mowing`` (``fleet.domain.rules.command_features``).
        """
        await self._check_features(account_id, request)
        base_update = request.command.kind == BASE_STATION_UPDATE
        if base_update:
            # The base station serves every mower linked to it (ADR 0047).
            await self.access.require(
                actor.principal, CAN_MANAGE_MOWERS, account(account_id), hide=False
            )
        now = self.clock()
        async with self.store.transaction() as tx:
            target = await tx.mowers.get(id=mower_id, account_id=account_id)
            if target is None:
                raise NotFoundError("mower not found")
            if base_update and target.base_station_id is None:
                raise BaseStationNotLinkedError("the mower is linked to no base station")
            envelope = self._envelope(actor, target, request, now)
            await tx.commands.add(self._record(envelope, origin=origin, schedule_id=schedule_id))
        if not await self._publish(envelope):
            raise Unavailable(NOT_PUBLISHED)
        return CommandAccepted(
            command_id=envelope.command_id,
            mower_id=envelope.mower_id,
            kind=envelope.command.kind,
            priority=envelope.priority,
            issued_at=envelope.issued_at,
            expires_at=envelope.expires_at,
            correlation_id=envelope.correlation_id,
            status_url=f"{self.config.status_url_prefix}{envelope.command_id}",
        )

    async def send_bulk(
        self,
        actor: Actor,
        account_id: UUID,
        target_type: Literal["site", "group"],
        target_id: UUID,
        request: CommandRequest,
    ) -> BulkCommandAccepted:
        """One command per mower of a site or group, each checked on its own.

        Allowed mowers get their own envelope and record (shared ``bulk_id``); mowers the
        caller sees but may not command are reported ``forbidden``; mowers the caller cannot
        see are omitted, so a group never reveals them.
        """
        kind = request.command.kind
        if kind == "spot_mow" or spot_plan_of(request.command) is not None:
            msg = "a spot plan is computed for one mower: send it to that mower"
            raise InvalidInput(msg)
        if kind in SINGLE_MOWER_KINDS:
            msg = f"{kind} is a command for one mower: send it to that mower"
            raise InvalidInput(msg)
        await self._check_features(account_id, request)
        if kind != EMERGENCY_STOP:
            await require_feature(self.entitlements, account_id, FEATURE_BULK)
        async with self.store.transaction() as tx:
            if target_type == "site":
                if await tx.sites.get(id=target_id, account_id=account_id) is None:
                    raise NotFoundError("site not found")
                members = await tx.mowers.find(
                    site_id=target_id, account_id=account_id, order_by="name"
                )
            else:
                if await tx.groups.get(id=target_id, account_id=account_id) is None:
                    raise NotFoundError("group not found")
                ids = [m.mower_id for m in await tx.mower_groups.find(group_id=target_id)]
                members = (
                    await tx.mowers.find(
                        id=In(sorted(ids, key=str)), account_id=account_id, order_by="name"
                    )
                    if ids
                    else []
                )
        if len(members) > self.config.bulk_max_mowers:
            msg = f"at most {self.config.bulk_max_mowers} mowers per bulk command"
            raise InvalidInput(msg)
        answers = await self.access.classify(
            actor.principal, relation_for(kind), [mower(m.id) for m in members]
        )
        bulk_id = uuid4()
        now = self.clock()
        items: list[BulkCommandItem] = []
        envelopes: list[CommandEnvelope] = []
        for target, (allowed, visible) in zip(members, answers, strict=True):
            if allowed:
                envelopes.append(self._envelope(actor, target, request, now))
            elif visible:
                items.append(
                    BulkCommandItem(mower_id=target.id, outcome=BulkCommandOutcome.FORBIDDEN)
                )
        if envelopes:
            async with self.store.transaction() as tx:
                for envelope in envelopes:
                    await tx.commands.add(self._record(envelope, bulk_id))
        for envelope in envelopes:
            published = await self._publish(envelope)
            items.append(
                BulkCommandItem(
                    mower_id=envelope.mower_id,
                    outcome=(
                        BulkCommandOutcome.ACCEPTED
                        if published
                        else BulkCommandOutcome.NOT_PUBLISHED
                    ),
                    command_id=envelope.command_id,
                    expires_at=envelope.expires_at,
                )
            )
        order = {m.id: index for index, m in enumerate(members)}
        items.sort(key=lambda item: order[item.mower_id])
        return BulkCommandAccepted(
            bulk_id=bulk_id, target_type=target_type, target_id=target_id, kind=kind, items=items
        )

    async def issue(self, request: IssueCommandRequest) -> IssueCommandReply:
        """``fleet.commands.issue``: a command on behalf of ``request.on_behalf_of``.

        The same checks as ``POST /mowers/{id}/commands`` for that subject: the relation of the
        command kind on the mower, ``forbidden`` when the subject sees the mower, ``not_found``
        when it does not (or the mower is not in ``account_id``). A refused command touches
        neither the database nor the bus. The record carries ``origin`` and ``schedule_id``.
        """
        ref = request.on_behalf_of
        if ref.kind == PrincipalKind.SERVICE:
            return _refused(IssueCommandErrorCode.INVALID, "service principals hold no rights")
        try:
            relation = relation_for(request.command.command.kind)
        except ValueError as exc:
            return _refused(IssueCommandErrorCode.INVALID, str(exc))
        principal = Principal(
            subject=ref.subject,
            account_id=request.account_id,
            kind=ref.kind,
            display_name=ref.display_name,
            token_id=ref.token_id,
        )
        actor = Actor(principal=principal, correlation_id=get_correlation_id())
        try:
            await self.access.require(principal, relation, mower(request.mower_id))
            accepted = await self.send(
                actor,
                request.account_id,
                request.mower_id,
                request.command,
                origin=request.origin,
                schedule_id=request.schedule_id,
            )
        except NotFoundError as exc:
            return _refused(IssueCommandErrorCode.NOT_FOUND, exc.message)
        except FeatureNotInPlanError as exc:
            return _refused(IssueCommandErrorCode.NOT_IN_PLAN, exc.message)
        except ForbiddenError as exc:
            return _refused(IssueCommandErrorCode.FORBIDDEN, exc.message)
        except (InvalidInput, BaseStationNotLinkedError) as exc:
            return _refused(IssueCommandErrorCode.INVALID, exc.message)
        except Unavailable as exc:
            return _refused(IssueCommandErrorCode.UNAVAILABLE, exc.message)
        return IssueCommandReply(accepted=accepted)

    # --- reading -------------------------------------------------------------------------

    async def get(self, actor: Actor, account_id: UUID, command_id: UUID) -> CommandView:
        """A record of the account whose mower the caller may view (404 otherwise)."""
        async with self.store.transaction() as tx:
            record = await tx.commands.get(command_id=command_id, account_id=account_id)
        if record is None:
            raise NotFoundError("command not found")
        try:
            await self.access.require(actor.principal, CAN_VIEW, mower(record.mower_id))
        except NotFoundError:
            raise NotFoundError("command not found") from None
        return view(record)

    async def list_for_mower(
        self,
        account_id: UUID,
        mower_id: UUID,
        *,
        before: datetime | None = None,
        limit: int = 50,
    ) -> CommandPage:
        """Records of one mower, newest first (keyset on ``issued_at``)."""
        where: dict[str, object] = {"account_id": account_id, "mower_id": mower_id}
        if before is not None:
            where["issued_at"] = Lt(before)
        async with self.store.transaction() as tx:
            if await tx.mowers.get(id=mower_id, account_id=account_id) is None:
                raise NotFoundError("mower not found")
            rows = await tx.commands.find(
                order_by="issued_at", descending=True, limit=limit + 1, **where
            )
        more = len(rows) > limit
        rows = rows[:limit]
        return CommandPage([view(r) for r in rows], rows[-1].issued_at if more else None)

    # --- results and retention -----------------------------------------------------------

    async def apply_result(self, result: CommandResult) -> bool:
        """Move the record to the result's status unless it is already there or further.

        Unknown command ids (for example from the development script) and results whose mower
        differs from the record are ignored. Returns True if a record changed.
        """
        status = result.status.value
        async with self.store.transaction() as tx:
            changed = await tx.commands.update_where(
                {"status": status, "reason": result.reason, "result_at": result.at},
                command_id=result.command_id,
                mower_id=result.mower_id,
                status=In(sorted(advances_from(status))),
            )
        return changed > 0

    async def purge(self) -> int:
        """Delete records older than the retention; one replica at a time (advisory lock)."""
        cutoff = self.clock() - self.config.command_retention
        async with self.store.transaction() as tx:
            if not await tx.try_lock(RETENTION_LOCK):
                return 0
            return await tx.commands.delete(issued_at=Lt(cutoff))
