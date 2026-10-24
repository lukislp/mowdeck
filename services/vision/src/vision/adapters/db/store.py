"""PostgreSQL store on SQLAlchemy Core (async, asyncpg).

A second open proposal for the same mower violates the partial unique index
``ux_proposals_open_mower`` and is reported as :class:`~vision.domain.errors.ConflictError`.
"""

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from mowdeck_contracts.v1 import PrincipalKind, PrincipalRef, VisionTask, VisionTrigger
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from vision.adapters.db import tables
from vision.domain.errors import ConflictError
from vision.domain.models import (
    FINAL_STATES,
    AssessmentOut,
    AutoSetting,
    FrameInfo,
    Proposal,
    ProposalAction,
    ProposalState,
    ProposalStep,
    ProviderRequest,
    RequestKind,
    StepOutcome,
    StoredImage,
    UserPreferences,
    UserProvider,
)

_REF_FIELDS = ("requested_by", "approved_by", "decided_by")


def _ref(value: Any) -> PrincipalRef | None:
    return PrincipalRef.model_validate(value) if value is not None else None


def proposal_values(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Proposal fields (domain types) to column values."""
    values: dict[str, Any] = {}
    for name, value in changes.items():
        if name in _REF_FIELDS:
            ref = value if value is None else PrincipalRef.model_validate(value)
            values[name] = ref.model_dump(mode="json") if ref else None
            if name in ("requested_by", "approved_by"):
                values[f"{name}_subject"] = ref.subject if ref else None
        elif name == "assessment":
            answer: AssessmentOut | None = value
            values["assessment"] = answer.model_dump(mode="json") if answer else None
            values["situation"] = answer.situation.value if answer else None
            values["confidence"] = answer.confidence if answer else None
        elif name in ("state", "task", "trigger", "action", "alternative_action"):
            values[name] = value.value if hasattr(value, "value") else value
        elif name == "frames":
            values[name] = [frame.to_json() for frame in value]
        else:
            values[name] = value
    return values


def proposal_of(row: Any) -> Proposal:
    """A ``proposals`` row as a domain object."""
    v = dict(row._mapping)
    return Proposal(
        id=v["id"],
        chain_id=v["chain_id"],
        round=v["round"],
        account_id=v["account_id"],
        mower_id=v["mower_id"],
        task=VisionTask(v["task"]),
        state=ProposalState(v["state"]),
        requested_by=PrincipalRef.model_validate(v["requested_by"]),
        created_at=v["created_at"],
        updated_at=v["updated_at"],
        parent_id=v["parent_id"],
        provider_model=v["provider_model"],
        provider_family=v["provider_family"],
        hosted=v["hosted"],
        assessment=AssessmentOut.model_validate(v["assessment"]) if v["assessment"] else None,
        context=v["context"] or {},
        image_count=v["image_count"],
        light_used=v["light_used"],
        stereo=v["stereo"],
        frames=[FrameInfo.from_json(f) for f in v["frames"] or []],
        trigger=VisionTrigger(v["trigger"]),
        trigger_error_code=v["trigger_error_code"],
        detail=v["detail"],
        analysed_at=v["analysed_at"],
        expires_at=v["expires_at"],
        approved_by=_ref(v["approved_by"]),
        approved_at=v["approved_at"],
        decided_by=_ref(v["decided_by"]),
        decided_at=v["decided_at"],
        cancel_requested_at=v["cancel_requested_at"],
        started_at=v["started_at"],
        finished_at=v["finished_at"],
        drive_session_id=v["drive_session_id"],
        start_pose=v["start_pose"],
        end_pose=v["end_pose"],
        return_command_id=v["return_command_id"],
        action=ProposalAction(v["action"]),
        fallback=v["fallback"],
        low_confidence=v["low_confidence"],
        alternative_action=ProposalAction(v["alternative_action"])
        if v["alternative_action"]
        else None,
        model_refused=v["model_refused"],
        approved_option=v["approved_option"],
    )


def _auto_of(row: Any) -> AutoSetting:
    v = dict(row._mapping)
    return AutoSetting(
        user_id=v["user_id"],
        account_id=v["account_id"],
        enabled=v["auto_analysis"],
        updated_at=v["updated_at"],
        kind=PrincipalKind(v["principal_kind"]),
    )


_PROPOSAL_FIELDS = [
    "id",
    "chain_id",
    "round",
    "account_id",
    "mower_id",
    "task",
    "state",
    "requested_by",
    "created_at",
    "updated_at",
    "parent_id",
    "provider_model",
    "provider_family",
    "hosted",
    "assessment",
    "context",
    "image_count",
    "light_used",
    "stereo",
    "frames",
    "trigger",
    "trigger_error_code",
    "detail",
    "analysed_at",
    "expires_at",
    "approved_by",
    "approved_at",
    "decided_by",
    "decided_at",
    "cancel_requested_at",
    "started_at",
    "finished_at",
    "drive_session_id",
    "start_pose",
    "end_pose",
    "return_command_id",
    "action",
    "fallback",
    "low_confidence",
    "alternative_action",
    "model_refused",
    "approved_option",
]


class SqlStore:
    """``Store`` port on PostgreSQL."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def add_proposal(self, proposal: Proposal, images: Sequence[StoredImage]) -> None:
        """Insert a proposal and its images in one transaction."""
        values = proposal_values({f: getattr(proposal, f) for f in _PROPOSAL_FIELDS})
        try:
            async with self.engine.begin() as conn:
                await conn.execute(sa.insert(tables.proposals).values(**values))
                if images:
                    await conn.execute(
                        sa.insert(tables.proposal_images),
                        [
                            {
                                "proposal_id": i.proposal_id,
                                "index": i.index,
                                "media_type": i.media_type,
                                "source": i.source,
                                "data": i.data,
                                "delete_after": i.delete_after,
                            }
                            for i in images
                        ],
                    )
        except IntegrityError as exc:
            raise ConflictError("another proposal for this mower is open") from exc

    async def get_proposal(self, proposal_id: UUID) -> Proposal | None:
        """A proposal by id."""
        p = tables.proposals
        async with self.engine.connect() as conn:
            row = (await conn.execute(sa.select(p).where(p.c.id == proposal_id))).first()
        return proposal_of(row) if row else None

    async def update_proposal(
        self,
        proposal_id: UUID,
        changes: Mapping[str, Any],
        *,
        states: Collection[ProposalState] | None = None,
    ) -> bool:
        """Conditional update."""
        p = tables.proposals
        statement = sa.update(p).where(p.c.id == proposal_id).values(**proposal_values(changes))
        if states is not None:
            statement = statement.where(p.c.state.in_([s.value for s in states]))
        try:
            async with self.engine.begin() as conn:
                return bool((await conn.execute(statement)).rowcount)
        except IntegrityError as exc:
            raise ConflictError("another proposal for this mower is open") from exc

    async def list_proposals(
        self, account_id: UUID, mower_id: UUID, *, limit: int, before: datetime | None = None
    ) -> list[Proposal]:
        """Newest first."""
        p = tables.proposals
        query = (
            sa.select(p)
            .where(p.c.account_id == account_id, p.c.mower_id == mower_id)
            .order_by(p.c.created_at.desc())
            .limit(limit)
        )
        if before is not None:
            query = query.where(p.c.created_at < before)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [proposal_of(r) for r in rows]

    async def open_proposal(self, mower_id: UUID) -> Proposal | None:
        """The newest non-final proposal of the mower."""
        p = tables.proposals
        query = (
            sa.select(p)
            .where(p.c.mower_id == mower_id, p.c.state.notin_([s.value for s in FINAL_STATES]))
            .order_by(p.c.created_at.desc())
            .limit(1)
        )
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return proposal_of(row) if row else None

    async def chain(self, chain_id: UUID) -> list[Proposal]:
        """Rounds of a session, oldest first."""
        p = tables.proposals
        query = sa.select(p).where(p.c.chain_id == chain_id).order_by(p.c.round)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [proposal_of(r) for r in rows]

    async def proposals_in(
        self, states: Collection[ProposalState], *, updated_before: datetime
    ) -> list[Proposal]:
        """For the sweeps (bounded batch)."""
        p = tables.proposals
        query = (
            sa.select(p)
            .where(p.c.state.in_([s.value for s in states]), p.c.updated_at < updated_before)
            .order_by(p.c.updated_at)
            .limit(500)
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [proposal_of(r) for r in rows]

    async def get_image(self, proposal_id: UUID, index: int) -> StoredImage | None:
        """One image."""
        i = tables.proposal_images
        query = sa.select(i).where(i.c.proposal_id == proposal_id, i.c.index == index)
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            return None
        v = row._mapping
        return StoredImage(
            v["proposal_id"], v["index"], v["media_type"], v["data"], v["source"], v["delete_after"]
        )

    async def purge_images(self, now: datetime) -> int:
        """Delete expired images."""
        i = tables.proposal_images
        async with self.engine.begin() as conn:
            result = await conn.execute(sa.delete(i).where(i.c.delete_after <= now))
        return int(result.rowcount or 0)

    async def put_steps(self, steps: Sequence[ProposalStep]) -> None:
        """Insert step rows."""
        if not steps:
            return
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.insert(tables.proposal_steps),
                [
                    {
                        "proposal_id": s.proposal_id,
                        "index": s.index,
                        "linear": s.linear,
                        "angular": s.angular,
                        "seconds": s.seconds,
                        "outcome": s.outcome.value,
                        "started_at": s.started_at,
                        "ended_at": s.ended_at,
                        "frames": s.frames,
                        "detail": s.detail,
                    }
                    for s in steps
                ],
            )

    async def update_step(self, proposal_id: UUID, index: int, changes: Mapping[str, Any]) -> None:
        """Update one step."""
        s = tables.proposal_steps
        values = {k: (v.value if isinstance(v, StepOutcome) else v) for k, v in changes.items()}
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.update(s)
                .where(s.c.proposal_id == proposal_id, s.c.index == index)
                .values(**values)
            )

    async def steps(self, proposal_id: UUID) -> list[ProposalStep]:
        """Steps in order."""
        s = tables.proposal_steps
        query = sa.select(s).where(s.c.proposal_id == proposal_id).order_by(s.c.index)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        result: list[ProposalStep] = []
        for row in rows:
            v = dict(row._mapping)
            v["outcome"] = StepOutcome(v["outcome"])
            result.append(ProposalStep(**v))
        return result

    async def record_request(self, request: ProviderRequest) -> None:
        """Insert one cost accounting row."""
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.insert(tables.provider_requests).values(
                    id=request.id,
                    account_id=request.account_id,
                    user_id=request.user_id,
                    proposal_id=request.proposal_id,
                    kind=request.kind,
                    model=request.model,
                    family=request.family,
                    hosted=request.hosted,
                    outcome=request.outcome,
                    input_tokens=request.input_tokens,
                    output_tokens=request.output_tokens,
                    cost_usd=request.cost_usd,
                    latency_ms=request.latency_ms,
                    detail=request.detail,
                    created_at=request.created_at,
                )
            )

    async def count_requests(
        self,
        *,
        since: datetime,
        kind: RequestKind,
        user_id: UUID | None = None,
        account_id: UUID | None = None,
        hosted: bool | None = None,
    ) -> int:
        """Count model calls."""
        r = tables.provider_requests
        query = (
            sa.select(sa.func.count())
            .select_from(r)
            .where(r.c.kind == kind, r.c.created_at >= since)
        )
        if user_id is not None:
            query = query.where(r.c.user_id == user_id)
        if account_id is not None:
            query = query.where(r.c.account_id == account_id)
        if hosted is not None:
            query = query.where(r.c.hosted == hosted)
        async with self.engine.connect() as conn:
            return int((await conn.execute(query)).scalar_one())

    async def get_provider(self, user_id: UUID, account_id: UUID) -> UserProvider | None:
        """A user's provider."""
        u = tables.user_providers
        query = sa.select(u).where(u.c.user_id == user_id, u.c.account_id == account_id)
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            return None
        v = dict(row._mapping)
        v["header_names"] = list(v["header_names"] or [])
        return UserProvider(**v)

    async def put_provider(self, provider: UserProvider) -> None:
        """Upsert."""
        values = {
            "user_id": provider.user_id,
            "account_id": provider.account_id,
            "model": provider.model,
            "enabled": provider.enabled,
            "api_base": provider.api_base,
            "api_key_ciphertext": provider.api_key_ciphertext,
            "headers_ciphertext": provider.headers_ciphertext,
            "header_names": list(provider.header_names),
            "last_test": provider.last_test,
            "created_at": provider.created_at,
            "updated_at": provider.updated_at,
        }
        statement = pg_insert(tables.user_providers).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "account_id"],
            set_={k: statement.excluded[k] for k in values if k not in ("user_id", "account_id")},
        )
        async with self.engine.begin() as conn:
            await conn.execute(statement)

    async def delete_provider(self, user_id: UUID, account_id: UUID) -> bool:
        """Delete a user's provider."""
        u = tables.user_providers
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.delete(u).where(u.c.user_id == user_id, u.c.account_id == account_id)
            )
        return bool(result.rowcount)

    async def delete_user(self, user_id: UUID) -> int:
        """Forget a user's providers and preferences (opt-ins included)."""
        u = tables.user_providers
        prefs = tables.user_preferences
        async with self.engine.begin() as conn:
            result = await conn.execute(sa.delete(u).where(u.c.user_id == user_id))
            await conn.execute(sa.delete(prefs).where(prefs.c.user_id == user_id))
        return int(result.rowcount or 0)

    async def get_preferences(self, user_id: UUID, account_id: UUID) -> UserPreferences | None:
        """A user's preferences."""
        prefs = tables.user_preferences
        query = sa.select(prefs).where(prefs.c.user_id == user_id, prefs.c.account_id == account_id)
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            return None
        v = row._mapping
        return UserPreferences(
            user_id=v["user_id"],
            account_id=v["account_id"],
            stereo_frames=v["stereo_frames"],
            updated_at=v["updated_at"],
        )

    async def put_preferences(self, preferences: UserPreferences) -> None:
        """Upsert the stereo preference; the opt-in columns of the row stay as they are."""
        values = {
            "user_id": preferences.user_id,
            "account_id": preferences.account_id,
            "stereo_frames": preferences.stereo_frames,
            "updated_at": preferences.updated_at,
        }
        statement = pg_insert(tables.user_preferences).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "account_id"],
            set_={
                "stereo_frames": statement.excluded.stereo_frames,
                "updated_at": statement.excluded.updated_at,
            },
        )
        async with self.engine.begin() as conn:
            await conn.execute(statement)

    # --- automatic analyses ---------------------------------------------------------------

    async def get_auto_setting(self, user_id: UUID, account_id: UUID) -> AutoSetting | None:
        """A user's opt-in (from ``user_preferences``)."""
        a = tables.user_preferences
        query = sa.select(a).where(a.c.user_id == user_id, a.c.account_id == account_id)
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return _auto_of(row) if row else None

    async def put_auto_setting(self, setting: AutoSetting) -> None:
        """Upsert a user's opt-in; other preferences of the row stay as they are."""
        values = {
            "user_id": setting.user_id,
            "account_id": setting.account_id,
            "auto_analysis": setting.enabled,
            "principal_kind": setting.kind.value,
            "updated_at": setting.updated_at,
        }
        statement = pg_insert(tables.user_preferences).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "account_id"],
            set_={
                k: statement.excluded[k] for k in ("auto_analysis", "principal_kind", "updated_at")
            },
        )
        async with self.engine.begin() as conn:
            await conn.execute(statement)

    async def auto_subscribers(self, account_id: UUID) -> list[AutoSetting]:
        """The users of the account who opted in, longest opted in first."""
        a = tables.user_preferences
        query = (
            sa.select(a)
            .where(a.c.account_id == account_id, a.c.auto_analysis.is_(True))
            .order_by(a.c.updated_at, a.c.user_id)
            .limit(100)
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [_auto_of(r) for r in rows]

    async def swap_auto_watch(
        self, mower_id: UUID, account_id: UUID, error_code: str | None, now: datetime
    ) -> str | None:
        """Remember ``error_code`` (None: no recoverable error) as the mower's current one and
        return the previous one (None for a new mower), atomically across replicas."""
        w = tables.auto_watch
        async with self.engine.begin() as conn:
            await conn.execute(
                pg_insert(w)
                .values(mower_id=mower_id, account_id=account_id, error_code=None, changed_at=now)
                .on_conflict_do_nothing(index_elements=["mower_id"])
            )
            previous = (
                await conn.execute(
                    sa.select(w.c.error_code).where(w.c.mower_id == mower_id).with_for_update()
                )
            ).scalar_one_or_none()
            if previous != error_code:
                await conn.execute(
                    sa.update(w)
                    .where(w.c.mower_id == mower_id)
                    .values(error_code=error_code, account_id=account_id, changed_at=now)
                )
        return previous

    async def count_proposals(
        self, mower_id: UUID, *, since: datetime, trigger: VisionTrigger | None = None
    ) -> int:
        """Proposals of the mower created since ``since`` (of one trigger, or any)."""
        p = tables.proposals
        query = (
            sa.select(sa.func.count())
            .select_from(p)
            .where(p.c.mower_id == mower_id, p.c.created_at >= since)
        )
        if trigger is not None:
            query = query.where(p.c.trigger == trigger.value)
        async with self.engine.connect() as conn:
            return int((await conn.execute(query)).scalar_one())
