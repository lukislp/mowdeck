"""Use cases: resolve an account's plan, answer checks, manage plans and assignments."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    UNLIMITED_PLAN,
    AccountCreated,
    EntitlementCheckReply,
    EntitlementCheckRequest,
    EntitlementSnapshot,
    PlanChanged,
    PlanSetError,
    PlanSetReply,
    PlanSetRequest,
    PlanSource,
    PlanStatus,
)
from mowdeck_service_kit import get_logger

from entitlements.application.catalogue import CatalogueService
from entitlements.application.ports import Clock, EventPublisher, Store, Tx
from entitlements.domain.errors import InvalidInput, NotFoundError
from entitlements.domain.models import AccountPlan, Plan, PlanHistory, unlimited_plan
from entitlements.domain.planfile import PlanSpec
from entitlements.domain.rules import (
    default_row,
    deny_all_plan,
    effective_status,
    snapshot,
    unknown_features,
)

_log = get_logger("entitlements.service")


@dataclass(frozen=True, slots=True)
class EntitlementsConfig:
    """The settings the use cases need (built from ``Settings`` in ``main``)."""

    billing_enabled: bool = False
    default_plan: str = "home"
    grace_days: int = 14
    history_limit: int = 100


@dataclass(frozen=True, slots=True)
class Resolved:
    """An account's plan as it applies now, with the row it came from (if any)."""

    snapshot: EntitlementSnapshot
    plan: Plan
    source: PlanSource
    row: AccountPlan | None = None


@dataclass(frozen=True, slots=True)
class AccountView:
    """An account row with its effective plan (operator list)."""

    row: AccountPlan
    resolved: Resolved


@dataclass(frozen=True, slots=True)
class AccountPage:
    """A page of accounts."""

    items: list[AccountView]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class Assignment:
    """What an operator assigns (``PUT /accounts/{id}/plan``)."""

    plan: str
    reason: str
    status: PlanStatus = PlanStatus.ACTIVE
    trial_until: datetime | None = None
    grace_until: datetime | None = None
    account_name: str | None = None


@dataclass(frozen=True, slots=True)
class AccountDetail:
    """One account: the resolved plan and the newest assignments."""

    resolved: Resolved
    history: list[PlanHistory] = field(default_factory=list)


class EntitlementsService:
    """Plans, assignments and decisions."""

    def __init__(
        self,
        store: Store,
        catalogue: CatalogueService,
        events: EventPublisher,
        config: EntitlementsConfig,
        clock: Clock,
    ) -> None:
        self.store = store
        self.catalogue = catalogue
        self.events = events
        self.config = config
        self.clock = clock

    # --- plans ---------------------------------------------------------------------------

    async def seed(self, specs: list[PlanSpec]) -> list[str]:
        """Upsert the plans of the plan file; returns the keys written.

        A plan is (re)written when it is new or its file entry changed since the last seed
        (``file_digest``); edits made through the API survive restarts until then. The built-in
        ``unlimited`` plan always exists.
        """
        now = self.clock()
        written: list[str] = []
        async with self.store.transaction() as tx:
            for spec in specs:
                digest = spec.digest()
                current = await tx.get_plan(spec.key)
                if current is not None and current.file_digest == digest:
                    continue
                plan = spec.to_plan(file_digest=digest)
                plan.created_at = current.created_at if current else now
                plan.updated_at = now
                await tx.save_plan(plan)
                if plan.is_default:
                    await tx.clear_default(plan.key)
                written.append(plan.key)
            if await tx.get_plan(UNLIMITED_PLAN) is None:
                builtin = unlimited_plan()
                builtin.created_at = builtin.updated_at = now
                await tx.save_plan(builtin)
                written.append(UNLIMITED_PLAN)
        await self.warn_unknown_features()
        return written

    async def warn_unknown_features(self) -> dict[str, list[str]]:
        """Log the feature keys of plans that no module declares (they are kept)."""
        known = await self.catalogue.known_keys()
        found: dict[str, list[str]] = {}
        for plan in await self.plans(include_inactive=True):
            unknown = unknown_features(plan, known)
            if unknown:
                found[plan.key] = unknown
        if found:
            _log.warning("plans list features no module declares (kept)", unknown=found)
        return found

    async def plans(self, *, include_inactive: bool = False) -> list[Plan]:
        """Every plan (``include_inactive``) or the offered ones."""
        async with self.store.transaction() as tx:
            plans = await tx.list_plans()
        return [p for p in plans if include_inactive or p.active]

    async def plan(self, key: str) -> Plan:
        """One plan, also an inactive one (404 when unknown)."""
        async with self.store.transaction() as tx:
            found = await tx.get_plan(key)
        if found is None:
            raise NotFoundError("plan not found")
        return found

    async def put_plan(self, spec: PlanSpec) -> Plan:
        """Create or replace a plan (operator); every client drops its cached snapshots."""
        now = self.clock()
        async with self.store.transaction() as tx:
            current = await tx.get_plan(spec.key)
            plan = spec.to_plan(file_digest=current.file_digest if current else None)
            plan.created_at = current.created_at if current else now
            plan.updated_at = now
            await tx.save_plan(plan)
            if plan.is_default:
                await tx.clear_default(plan.key)
        await self.events.plan_changed(
            PlanChanged(plan=plan.key, all_accounts=True, reason="plan definition changed")
        )
        return plan

    async def unknown_features_of(self, plan: Plan) -> list[str]:
        """Feature keys of ``plan`` that no module declares."""
        return unknown_features(plan, await self.catalogue.known_keys())

    # --- resolution ----------------------------------------------------------------------

    async def _default_plan(self, tx: Tx) -> Plan:
        found = await tx.get_plan(self.config.default_plan)
        if found is not None:
            return found
        for plan in await tx.list_plans():
            if plan.is_default:
                return plan
        _log.error("the default plan does not exist", plan=self.config.default_plan)
        return deny_all_plan(self.config.default_plan)

    async def _resolve(self, tx: Tx, account_id: UUID) -> Resolved:
        now = self.clock()
        enabled = self.config.billing_enabled
        row = await tx.get_account(account_id)
        if not enabled:
            plan = await tx.get_plan(UNLIMITED_PLAN) or unlimited_plan()
            return Resolved(
                snapshot(account_id, plan, PlanStatus.ACTIVE, billing_enabled=False),
                plan,
                PlanSource.DEFAULT,
                row,
            )
        if row is None:
            plan = await self._default_plan(tx)
            return Resolved(
                snapshot(account_id, plan, PlanStatus.ACTIVE, billing_enabled=True),
                plan,
                PlanSource.DEFAULT,
            )
        assigned = await tx.get_plan(row.plan_key)
        if assigned is None:
            _log.error("an account holds an unknown plan", plan=row.plan_key)
            assigned = await self._default_plan(tx)
        status = effective_status(row, now)
        return Resolved(
            snapshot(account_id, assigned, status, billing_enabled=True),
            assigned,
            row.source,
            row,
        )

    async def resolve(self, account_id: UUID) -> Resolved:
        """The plan that applies to the account now."""
        async with self.store.transaction() as tx:
            return await self._resolve(tx, account_id)

    async def check(self, request: EntitlementCheckRequest) -> EntitlementCheckReply:
        """``entitlements.check``: decide the question with the account's snapshot."""
        resolved = await self.resolve(request.account_id)
        return resolved.snapshot.decide(request.feature, request.limit)

    # --- accounts ------------------------------------------------------------------------

    async def account_created(self, event: AccountCreated) -> bool:
        """Give a new account the default plan (a trial when the plan has trial days).

        Idempotent: an account that already has a row keeps it. Rows are written whether
        billing is enabled or not, so enabling billing later finds every account.
        """
        now = self.clock()
        async with self.store.transaction() as tx:
            plan = await self._default_plan(tx)
            row = default_row(event.account_id, plan, now)
            row.account_name = event.name
            inserted = await tx.add_account_if_absent(row)
            if inserted:
                await tx.add_history(
                    PlanHistory(
                        id=uuid4(),
                        account_id=row.account_id,
                        at=now,
                        plan_key=row.plan_key,
                        status=row.status,
                        source=row.source,
                        reason="account created",
                        trial_until=row.trial_until,
                    )
                )
        return inserted

    async def accounts(self, offset: int, limit: int) -> AccountPage:
        """A page of accounts with a row, with their effective plans."""
        async with self.store.transaction() as tx:
            rows = await tx.list_accounts(offset, limit)
            total = await tx.count_accounts()
            items = [AccountView(row, await self._resolve(tx, row.account_id)) for row in rows]
        return AccountPage(items, total, offset, limit)

    async def account(self, account_id: UUID) -> AccountDetail:
        """The resolved plan and the history of one account (any account id: operators)."""
        async with self.store.transaction() as tx:
            resolved = await self._resolve(tx, account_id)
            history = await tx.history(account_id, self.config.history_limit)
        return AccountDetail(resolved, history)

    async def assign(self, account_id: UUID, data: Assignment, actor: str | None) -> AccountDetail:
        """Assign a plan by hand (``source=manual``), record it and tell every client."""
        now = self.clock()
        reason = data.reason.strip()
        if not reason:
            raise InvalidInput("a reason is required")
        async with self.store.transaction() as tx:
            plan = await tx.get_plan(data.plan)
            if plan is None:
                raise InvalidInput(f"unknown plan {data.plan!r}")
            trial_until, grace_until = data.trial_until, data.grace_until
            if data.status is PlanStatus.TRIAL and trial_until is None:
                if plan.trial_days <= 0:
                    raise InvalidInput("the plan has no trial; give trial_until")
                trial_until = now + timedelta(days=plan.trial_days)
            if data.status is PlanStatus.GRACE and grace_until is None:
                grace_until = now + timedelta(days=self.config.grace_days)
            current = await tx.get_account(account_id)
            row = AccountPlan(
                account_id=account_id,
                plan_key=plan.key,
                status=data.status,
                since=now,
                source=PlanSource.MANUAL,
                updated_at=now,
                account_name=data.account_name or (current.account_name if current else None),
                trial_until=trial_until if data.status is PlanStatus.TRIAL else None,
                grace_until=grace_until if data.status is PlanStatus.GRACE else None,
            )
            await tx.save_account(row)
            await tx.add_history(
                PlanHistory(
                    id=uuid4(),
                    account_id=account_id,
                    at=now,
                    plan_key=plan.key,
                    status=row.status,
                    source=row.source,
                    reason=reason[:500],
                    actor=actor,
                    trial_until=row.trial_until,
                    grace_until=row.grace_until,
                )
            )
            resolved = await self._resolve(tx, account_id)
            history = await tx.history(account_id, self.config.history_limit)
        await self.events.plan_changed(
            PlanChanged(
                account_id=account_id,
                plan=plan.key,
                status=resolved.snapshot.status,
                reason=reason[:500],
            )
        )
        _log.info(
            "plan assigned",
            account_id=str(account_id),
            plan=plan.key,
            status=row.status.value,
            actor=actor,
        )
        return AccountDetail(resolved, history)

    async def set_from_billing(self, request: PlanSetRequest) -> PlanSetReply:
        """``entitlements.plan.set``: Billing sets an account's plan (``source=billing``).

        A trial without ``trial_until`` lasts the plan's trial days, a grace period without
        ``grace_until`` lasts ``grace_days``. A request equal to the stored row (same plan,
        status, dates and source) changes nothing: Billing may repeat it after a retry.
        """
        now = self.clock()
        reason = request.reason.strip()[:500]
        async with self.store.transaction() as tx:
            plan = await tx.get_plan(request.plan)
            if plan is None:
                return PlanSetReply(
                    ok=False,
                    error=PlanSetError.UNKNOWN_PLAN,
                    detail=f"unknown plan {request.plan!r}",
                )
            trial_until = request.trial_until if request.status is PlanStatus.TRIAL else None
            grace_until = request.grace_until if request.status is PlanStatus.GRACE else None
            if request.status is PlanStatus.TRIAL and trial_until is None:
                trial_until = now + timedelta(days=max(plan.trial_days, 0))
            if request.status is PlanStatus.GRACE and grace_until is None:
                grace_until = now + timedelta(days=self.config.grace_days)
            current = await tx.get_account(request.account_id)
            unchanged = (
                current is not None
                and current.source is PlanSource.BILLING
                and current.plan_key == plan.key
                and current.status is request.status
                and current.trial_until == trial_until
                and current.grace_until == grace_until
            )
            if not unchanged:
                row = AccountPlan(
                    account_id=request.account_id,
                    plan_key=plan.key,
                    status=request.status,
                    since=now,
                    source=PlanSource.BILLING,
                    updated_at=now,
                    account_name=request.account_name
                    or (current.account_name if current else None),
                    trial_until=trial_until,
                    grace_until=grace_until,
                )
                await tx.save_account(row)
                await tx.add_history(
                    PlanHistory(
                        id=uuid4(),
                        account_id=request.account_id,
                        at=now,
                        plan_key=plan.key,
                        status=row.status,
                        source=row.source,
                        reason=reason,
                        actor=f"service:billing:{request.reference}"[:100]
                        if request.reference
                        else "service:billing",
                        trial_until=trial_until,
                        grace_until=grace_until,
                    )
                )
            resolved = await self._resolve(tx, request.account_id)
        if not unchanged:
            await self.events.plan_changed(
                PlanChanged(
                    account_id=request.account_id,
                    plan=plan.key,
                    status=resolved.snapshot.status,
                    reason=reason,
                )
            )
            _log.info(
                "plan set by billing",
                account_id=str(request.account_id),
                plan=plan.key,
                status=request.status.value,
            )
        return PlanSetReply(
            ok=True, plan=plan.key, status=resolved.snapshot.status, changed=not unchanged
        )

    async def announce_restart(self) -> None:
        """Tell every client to drop its snapshots (settings such as billing may have changed)."""
        await self.events.plan_changed(
            PlanChanged(all_accounts=True, reason="entitlements started")
        )
