"""In-memory ``Store`` for tests and the OpenAPI export (same semantics as the SQL store)."""

import copy
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import PrincipalRef, VisionTrigger

from vision.domain.errors import ConflictError
from vision.domain.models import (
    FINAL_STATES,
    AutoSetting,
    Proposal,
    ProposalState,
    ProposalStep,
    ProviderRequest,
    RequestKind,
    StoredImage,
    UserPreferences,
    UserProvider,
)

_REF_FIELDS = frozenset({"requested_by", "approved_by", "decided_by"})


class MemoryStore:
    """``Store`` port in dictionaries."""

    def __init__(self) -> None:
        self.proposals: dict[UUID, Proposal] = {}
        self.images: dict[tuple[UUID, int], StoredImage] = {}
        self.step_rows: dict[tuple[UUID, int], ProposalStep] = {}
        self.requests: list[ProviderRequest] = []
        self.providers: dict[tuple[UUID, UUID], UserProvider] = {}
        self.preferences: dict[tuple[UUID, UUID], UserPreferences] = {}
        self.auto: dict[tuple[UUID, UUID], AutoSetting] = {}
        self.watch: dict[UUID, str | None] = {}
        self.watch_accounts: dict[UUID, UUID] = {}

    def _open_conflict(self, mower_id: UUID, exclude: UUID | None = None) -> bool:
        return any(
            p.mower_id == mower_id and p.state not in FINAL_STATES and p.id != exclude
            for p in self.proposals.values()
        )

    async def add_proposal(self, proposal: Proposal, images: Sequence[StoredImage]) -> None:
        if proposal.state not in FINAL_STATES and self._open_conflict(proposal.mower_id):
            raise ConflictError("another proposal for this mower is open")
        self.proposals[proposal.id] = copy.deepcopy(proposal)
        for image in images:
            self.images[(image.proposal_id, image.index)] = image

    async def get_proposal(self, proposal_id: UUID) -> Proposal | None:
        found = self.proposals.get(proposal_id)
        return copy.deepcopy(found) if found else None

    async def update_proposal(
        self,
        proposal_id: UUID,
        changes: Mapping[str, Any],
        *,
        states: Collection[ProposalState] | None = None,
    ) -> bool:
        current = self.proposals.get(proposal_id)
        if current is None or (states is not None and current.state not in states):
            return False
        values = dict(changes)
        for name in _REF_FIELDS & set(values):
            if values[name] is not None:
                values[name] = PrincipalRef.model_validate(values[name])
        updated = replace(current, **values)
        if updated.state not in FINAL_STATES and self._open_conflict(
            updated.mower_id, exclude=proposal_id
        ):
            raise ConflictError("another proposal for this mower is open")
        self.proposals[proposal_id] = updated
        return True

    async def list_proposals(
        self, account_id: UUID, mower_id: UUID, *, limit: int, before: datetime | None = None
    ) -> list[Proposal]:
        rows = [
            p
            for p in self.proposals.values()
            if p.account_id == account_id
            and p.mower_id == mower_id
            and (before is None or p.created_at < before)
        ]
        rows.sort(key=lambda p: p.created_at, reverse=True)
        return [copy.deepcopy(p) for p in rows[:limit]]

    async def open_proposal(self, mower_id: UUID) -> Proposal | None:
        rows = [
            p
            for p in self.proposals.values()
            if p.mower_id == mower_id and p.state not in FINAL_STATES
        ]
        rows.sort(key=lambda p: p.created_at, reverse=True)
        return copy.deepcopy(rows[0]) if rows else None

    async def chain(self, chain_id: UUID) -> list[Proposal]:
        rows = [p for p in self.proposals.values() if p.chain_id == chain_id]
        rows.sort(key=lambda p: p.round)
        return [copy.deepcopy(p) for p in rows]

    async def proposals_in(
        self, states: Collection[ProposalState], *, updated_before: datetime
    ) -> list[Proposal]:
        return [
            copy.deepcopy(p)
            for p in self.proposals.values()
            if p.state in states and p.updated_at < updated_before
        ]

    async def get_image(self, proposal_id: UUID, index: int) -> StoredImage | None:
        return self.images.get((proposal_id, index))

    async def purge_images(self, now: datetime) -> int:
        expired = [key for key, image in self.images.items() if image.delete_after <= now]
        for key in expired:
            del self.images[key]
        return len(expired)

    async def put_steps(self, steps: Sequence[ProposalStep]) -> None:
        for step in steps:
            self.step_rows[(step.proposal_id, step.index)] = copy.deepcopy(step)

    async def update_step(self, proposal_id: UUID, index: int, changes: Mapping[str, Any]) -> None:
        current = self.step_rows.get((proposal_id, index))
        if current is not None:
            self.step_rows[(proposal_id, index)] = replace(current, **dict(changes))

    async def steps(self, proposal_id: UUID) -> list[ProposalStep]:
        rows = [s for (pid, _), s in self.step_rows.items() if pid == proposal_id]
        return [copy.deepcopy(s) for s in sorted(rows, key=lambda s: s.index)]

    async def record_request(self, request: ProviderRequest) -> None:
        self.requests.append(request)

    async def count_requests(
        self,
        *,
        since: datetime,
        kind: RequestKind,
        user_id: UUID | None = None,
        account_id: UUID | None = None,
        hosted: bool | None = None,
    ) -> int:
        return sum(
            1
            for r in self.requests
            if r.kind == kind
            and r.created_at >= since
            and (user_id is None or r.user_id == user_id)
            and (account_id is None or r.account_id == account_id)
            and (hosted is None or r.hosted == hosted)
        )

    async def get_provider(self, user_id: UUID, account_id: UUID) -> UserProvider | None:
        found = self.providers.get((user_id, account_id))
        return copy.deepcopy(found) if found else None

    async def put_provider(self, provider: UserProvider) -> None:
        self.providers[(provider.user_id, provider.account_id)] = copy.deepcopy(provider)

    async def delete_provider(self, user_id: UUID, account_id: UUID) -> bool:
        return self.providers.pop((user_id, account_id), None) is not None

    async def delete_user(self, user_id: UUID) -> int:
        keys = [k for k in self.providers if k[0] == user_id]
        for key in keys:
            del self.providers[key]
        for key in [k for k in self.preferences if k[0] == user_id]:
            del self.preferences[key]
        for key in [k for k in self.auto if k[0] == user_id]:
            del self.auto[key]
        return len(keys)

    async def get_preferences(self, user_id: UUID, account_id: UUID) -> UserPreferences | None:
        found = self.preferences.get((user_id, account_id))
        return copy.deepcopy(found) if found else None

    async def put_preferences(self, preferences: UserPreferences) -> None:
        key = (preferences.user_id, preferences.account_id)
        self.preferences[key] = copy.deepcopy(preferences)

    async def get_auto_setting(self, user_id: UUID, account_id: UUID) -> AutoSetting | None:
        return self.auto.get((user_id, account_id))

    async def put_auto_setting(self, setting: AutoSetting) -> None:
        self.auto[(setting.user_id, setting.account_id)] = setting

    async def auto_subscribers(self, account_id: UUID) -> list[AutoSetting]:
        rows = [s for s in self.auto.values() if s.account_id == account_id and s.enabled]
        return sorted(rows, key=lambda s: (s.updated_at, str(s.user_id)))

    async def swap_auto_watch(
        self, mower_id: UUID, account_id: UUID, error_code: str | None, now: datetime
    ) -> str | None:
        previous = self.watch.get(mower_id)
        self.watch[mower_id] = error_code
        if mower_id not in self.watch_accounts or previous != error_code:
            self.watch_accounts[mower_id] = account_id  # as the SQL store writes the column
        return previous

    async def purge_account(self, account_id: UUID) -> int:
        gone = {k for k, p in self.proposals.items() if p.account_id == account_id}
        watched = {m for m, a in self.watch_accounts.items() if a == account_id}
        before = self._rows()
        self.proposals = {k: v for k, v in self.proposals.items() if k not in gone}
        self.images = {k: v for k, v in self.images.items() if k[0] not in gone}
        self.step_rows = {k: v for k, v in self.step_rows.items() if k[0] not in gone}
        self.requests = [r for r in self.requests if r.account_id != account_id]
        self.providers = {k: v for k, v in self.providers.items() if k[1] != account_id}
        self.preferences = {k: v for k, v in self.preferences.items() if k[1] != account_id}
        self.auto = {k: v for k, v in self.auto.items() if k[1] != account_id}
        self.watch = {k: v for k, v in self.watch.items() if k not in watched}
        self.watch_accounts = {k: v for k, v in self.watch_accounts.items() if k not in watched}
        return before - self._rows()

    def _rows(self) -> int:
        """Rows as the SQL store counts them (``user_preferences`` holds both preferences and
        opt-ins: one row per user and account)."""
        settings = set(self.preferences) | set(self.auto)
        return (
            len(self.proposals)
            + len(self.images)
            + len(self.step_rows)
            + len(self.requests)
            + len(self.providers)
            + len(settings)
            + len(self.watch)
        )

    async def count_proposals(
        self, mower_id: UUID, *, since: datetime, trigger: VisionTrigger | None = None
    ) -> int:
        return sum(
            1
            for p in self.proposals.values()
            if p.mower_id == mower_id
            and p.created_at >= since
            and (trigger is None or p.trigger is trigger)
        )
