"""Strict server-side validation of a model's answer against the catalog (ADR 0075).

The answer is untrusted: it comes from a model that read untrusted text. Whatever it contains,
only the kinds and areas of the catalog survive, times are checked against the clock, and
anything unexpected rejects the whole proposal (all or nothing)."""

import re
from datetime import datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, ValidationError

from assistant.domain.model import (
    DURATION_DEFAULT,
    DURATION_RANGE,
    IMMEDIATE,
    LATER_MAX_DAYS,
    LATER_MIN_LEAD_SECONDS,
    MAX_ACTIONS,
    MAX_AREAS,
    MAX_LATER,
    ActionKind,
    Catalog,
    Interpretation,
    Note,
    ProposedAction,
    RejectedAction,
    Rejection,
)

_LOCAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")

Short = Annotated[str, StringConstraints(max_length=128)]


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Annotated[str, StringConstraints(max_length=32)]
    area_ids: list[Short] = Field(max_length=4 * MAX_AREAS)
    start_at: Annotated[str, StringConstraints(max_length=32)] | None
    duration_minutes: StrictInt | None


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    actions: list[_Action] = Field(max_length=10)
    notes: list[Annotated[str, StringConstraints(max_length=32)]] = Field(max_length=10)


def _notes(raw: list[str]) -> tuple[Note, ...]:
    known = {n.value for n in Note}
    found: list[Note] = []
    for value in raw:
        if value in known and Note(value) not in found:
            found.append(Note(value))
    return tuple(found)


def interpret(answer: Any, catalog: Catalog) -> Interpretation:
    """Validate ``answer`` (the model's JSON object) against ``catalog``."""
    try:
        parsed = _Answer.model_validate(answer)
    except ValidationError:
        return Interpretation((), (RejectedAction(Rejection.MALFORMED),))
    notes = _notes(parsed.notes)
    if len(parsed.actions) > MAX_ACTIONS:
        return Interpretation((), (RejectedAction(Rejection.TOO_MANY_ACTIONS),), notes)
    actions: list[ProposedAction] = []
    rejected: list[RejectedAction] = []
    for raw in parsed.actions:
        outcome = _action(raw, catalog)
        if isinstance(outcome, RejectedAction):
            rejected.append(outcome)
        else:
            actions.append(outcome)
    if not rejected:
        immediate = [a for a in actions if a.kind in IMMEDIATE]
        later = [a for a in actions if a.kind is ActionKind.START_LATER]
        if len(immediate) > 1 or len(later) > MAX_LATER:
            rejected.append(RejectedAction(Rejection.CONFLICT))
    if rejected:
        return Interpretation((), tuple(rejected), notes)
    return Interpretation(tuple(actions), (), notes)


def _action(raw: _Action, catalog: Catalog) -> ProposedAction | RejectedAction:
    try:
        kind = ActionKind(raw.kind)
    except ValueError:
        return RejectedAction(Rejection.UNKNOWN_KIND)
    if kind not in catalog.offered:
        return RejectedAction(Rejection.NOT_OFFERED, kind)
    if kind in (ActionKind.START_NOW, ActionKind.START_LATER):
        areas = _areas(raw.area_ids, catalog)
        if isinstance(areas, Rejection):
            return RejectedAction(areas, kind)
    elif raw.area_ids:
        return RejectedAction(Rejection.UNEXPECTED_FIELD, kind)
    else:
        areas = ()
    if kind is not ActionKind.START_LATER:
        if raw.start_at is not None or raw.duration_minutes is not None:
            return RejectedAction(Rejection.UNEXPECTED_FIELD, kind)
        return ProposedAction(kind, areas)
    if raw.start_at is None:
        return RejectedAction(Rejection.TIME_MISSING, kind)
    when = _local_time(raw.start_at, catalog)
    if isinstance(when, Rejection):
        return RejectedAction(when, kind)
    duration = DURATION_DEFAULT if raw.duration_minutes is None else raw.duration_minutes
    if not DURATION_RANGE[0] <= duration <= DURATION_RANGE[1]:
        return RejectedAction(Rejection.DURATION_INVALID, kind)
    return ProposedAction(kind, areas, when, raw.start_at, duration)


def _areas(raw: list[str], catalog: Catalog) -> tuple[str, ...] | Rejection:
    """The named areas in order (duplicates dropped); all areas for an empty list."""
    if not raw:
        return tuple(a.id for a in catalog.areas)
    chosen: list[str] = []
    for area_id in raw:
        if catalog.area(area_id) is None:
            return Rejection.UNKNOWN_AREA
        if area_id not in chosen:
            chosen.append(area_id)
    if len(chosen) > MAX_AREAS:
        return Rejection.TOO_MANY_AREAS
    return tuple(chosen)


def _local_time(value: str, catalog: Catalog) -> datetime | Rejection:
    """The UTC instant of a local ``YYYY-MM-DDTHH:MM`` within the allowed window."""
    if not _LOCAL.match(value):
        return Rejection.TIME_INVALID
    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        return Rejection.TIME_INVALID
    instant = naive.replace(tzinfo=ZoneInfo(catalog.timezone), fold=0).astimezone(
        catalog.now.tzinfo
    )
    if instant < catalog.now + timedelta(seconds=LATER_MIN_LEAD_SECONDS):
        return Rejection.TIME_PAST
    if instant > catalog.now + timedelta(days=LATER_MAX_DAYS):
        return Rejection.TIME_TOO_FAR
    return instant
