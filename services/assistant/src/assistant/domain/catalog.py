"""The catalog of one request: which action kinds and areas the model may choose from."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from assistant.domain.model import (
    ACTIVITIES,
    RELATIONS,
    ActionKind,
    Area,
    Catalog,
    Unavailable,
)

MAX_NAME = 60
"""Area and mower names are cut to this length before they reach the model."""


def clean_name(value: str | None, fallback: str) -> str:
    """A name as one line of at most :data:`MAX_NAME` printable characters (names are data
    typed by people or reported by the mower; they go to the model as JSON strings)."""
    text = " ".join("".join(c if c.isprintable() else " " for c in (value or "")).split())
    return text[:MAX_NAME] or fallback


def areas_of(
    map_areas: Sequence[tuple[str, str | None]], custom_names: Mapping[str, str]
) -> tuple[Area, ...]:
    """The map areas in map order with the names the web app shows: the custom name, else the
    mower's own name, else ``Area N``."""
    return tuple(
        Area(area_id, clean_name(custom_names.get(area_id) or own, f"Area {index}"))
        for index, (area_id, own) in enumerate(map_areas, start=1)
    )


def build_catalog(
    *,
    mower_id: UUID,
    mower_name: str,
    now: datetime,
    timezone: str,
    relations: Mapping[str, bool],
    connected: bool,
    areas: tuple[Area, ...],
    activity: str | None,
) -> Catalog:
    """Offer each kind the caller may use on this mower in its current situation.

    ``connected`` is false when the mower never reported its capabilities (its connection
    never came up): nothing is offered then. Starts need map areas. Immediate kinds follow
    the activity when it is known (:data:`~assistant.domain.model.ACTIVITIES`)."""
    offered: set[ActionKind] = set()
    unavailable: dict[ActionKind, Unavailable] = {}
    for kind in ActionKind:
        if not all(relations.get(r, False) for r in RELATIONS[kind]):
            unavailable[kind] = Unavailable.PERMISSION
        elif not connected:
            unavailable[kind] = Unavailable.NOT_CONNECTED
        elif kind in (ActionKind.START_NOW, ActionKind.START_LATER) and not areas:
            unavailable[kind] = Unavailable.NO_AREAS
        elif (allowed := ACTIVITIES[kind]) is not None and activity and activity not in allowed:
            if activity in ("unknown", "offline"):
                offered.add(kind)
            else:
                unavailable[kind] = Unavailable.STATE
        else:
            offered.add(kind)
    return Catalog(
        mower_id=mower_id,
        mower_name=clean_name(mower_name, "Mower"),
        now=now,
        timezone=timezone,
        areas=areas,
        offered=frozenset(offered),
        unavailable=unavailable,
        activity=activity,
    )
