"""Request and response bodies of the assistant API (``/api/v1/assistant``)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from assistant.application.ports import InterpreterStatus
from assistant.application.proposals import Proposal
from assistant.domain.model import (
    ActionKind,
    Catalog,
    Note,
    ProposedAction,
    Rejection,
    Unavailable,
)
from assistant.domain.schema import TEXT_MAX


class StatusOut(BaseModel):
    """``GET /status``: whether the caller can ask now."""

    ready: bool
    interpreter: Literal["provider", "phrases"] = Field(
        description="`provider`: the caller's AI provider (Settings, AI provider); `phrases`: "
        "the built-in phrase parser without AI (demo mode or the operator's choice)."
    )
    reason: Literal["no_provider", "provider_unavailable"] | None = Field(
        description="Why not ready: no provider configured (own or the installation's), or "
        "the AI provider settings (Vision) are not reachable."
    )
    model: str | None = Field(description="The model a request would use (never a key).")
    hosted: bool | None = Field(description="The installation's model, not the caller's own.")

    @classmethod
    def of(
        cls, status: InterpreterStatus, interpreter: Literal["provider", "phrases"]
    ) -> "StatusOut":
        """From the use case result."""
        return cls(
            ready=status.ready,
            interpreter=interpreter,
            reason=status.reason,
            model=status.model,
            hosted=status.hosted,
        )


class ProposalIn(BaseModel):
    """``POST /mowers/{mower_id}/proposals``: one request in the user's words."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=TEXT_MAX, description="What the user typed.")
    timezone: str = Field(
        min_length=1,
        max_length=64,
        description='IANA time zone of the user (the browser\'s): times like "tomorrow 8:00" '
        "are meant in it, and a one-off start is scheduled in it.",
    )


class AreaOut(BaseModel):
    """A map area of the mower."""

    id: str
    name: str


class ActionOut(BaseModel):
    """One validated action: exactly what the web app executes after the user confirmed."""

    kind: ActionKind
    area_ids: list[str] = Field(
        description="`start_now`/`start_later`: the areas in mowing order (every area when "
        "the user named none); empty for the other kinds."
    )
    areas: list[AreaOut]
    start_at: datetime | None = Field(description="`start_later`: the UTC start.")
    start_local: str | None = Field(
        description="`start_later`: the start as `YYYY-MM-DDTHH:MM` in `timezone`."
    )
    timezone: str | None
    duration_minutes: int | None = Field(description="`start_later`: the time window.")

    @classmethod
    def of(cls, action: ProposedAction, catalog: Catalog) -> "ActionOut":
        """From the domain value."""
        later = action.start_at is not None
        return cls(
            kind=action.kind,
            area_ids=list(action.area_ids),
            areas=[
                AreaOut(id=a.id, name=a.name)
                for a in (catalog.area(i) for i in action.area_ids)
                if a is not None
            ],
            start_at=action.start_at,
            start_local=action.start_local,
            timezone=catalog.timezone if later else None,
            duration_minutes=action.duration_minutes,
        )


class RejectedOut(BaseModel):
    """An action of the interpretation that the validator refused."""

    code: Rejection
    kind: ActionKind | None


class ProposalOut(BaseModel):
    """The validated proposal for one request. Nothing has been executed.

    ``executable`` is true only when every action passed validation (all or nothing); then the
    web app shows the actions on a confirmation card and, after the user confirmed, sends them
    through Fleet's command and the Scheduler's schedule endpoints with the user's token.
    """

    mower_id: UUID
    mower_name: str
    interpreter: Literal["provider", "phrases"]
    model: str | None
    executable: bool
    actions: list[ActionOut]
    rejected: list[RejectedOut]
    notes: list[Note] = Field(description="Why (part of) the request has no action.")
    unavailable: dict[ActionKind, Unavailable] = Field(
        description="Kinds not offered for this mower right now, with the reason."
    )
    areas: list[AreaOut] = Field(description="Every map area of the mower.")
    created_at: datetime
    expires_at: datetime = Field(description="Execute before this time, else ask again.")

    @classmethod
    def of(cls, proposal: Proposal) -> "ProposalOut":
        """From the use case result."""
        catalog = proposal.catalog
        result = proposal.interpretation
        return cls(
            mower_id=catalog.mower_id,
            mower_name=catalog.mower_name,
            interpreter=proposal.interpreter,
            model=proposal.model,
            executable=result.executable,
            actions=[ActionOut.of(a, catalog) for a in result.actions],
            rejected=[RejectedOut(code=r.code, kind=r.kind) for r in result.rejected],
            notes=list(result.notes),
            unavailable=dict(catalog.unavailable),
            areas=[AreaOut(id=a.id, name=a.name) for a in catalog.areas],
            created_at=proposal.created_at,
            expires_at=proposal.expires_at,
        )
