"""Display names of map areas and the event of custom area names.

Fleet stores a custom name per mower and area id (the mower keeps its own names; MowDeck never
writes to the mower). Every place that shows an area resolves its name the same way
(:func:`area_display_name`):

1. the custom name from Fleet (``MowerOut.area_names``, the event below),
2. else the mower's own name (``MapArea.name``) unless it is empty or only a long number (a
   vendor hash such as ``"4525245168006937683"``),
3. else a numbered fallback like the vendor app: ``"Area N"`` / ``"Bereich N"`` with ``N`` the
   1-based position of the area in the map's area order. The order is the order of
   ``MowerMap.areas`` in the snapshot, so the number stays the same for every viewer of one map
   snapshot and only changes when the mower's map itself changes.

The web app implements the same rule in TypeScript (``$lib/mower/map-labels.ts``) and the Home
Assistant integration in ``coordinator.py`` (it cannot import this package).
"""

import re
from collections.abc import Mapping, Sequence
from typing import Final
from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel
from mowdeck_contracts.v1.maps import MapArea, MowerMap
from mowdeck_contracts.v1.text import clean_text

FLEET_AREA_NAMES_CHANGED: Final = "fleet.mower.area_names_changed"
"""Event type: the custom area names of a mower changed (payload :class:`AreaNamesChanged`)."""

AREA_NAME_MAX_LENGTH: Final = 60
"""Longest custom area name (after trimming)."""

AREA_FALLBACK: Final[Mapping[str, str]] = {"en": "Area {n}", "de": "Bereich {n}"}
"""The numbered fallback per language; other languages use English."""

_HASH_NAME = re.compile(r"^\d{8,}$")


class AreaNamesChanged(ContractModel):
    """Payload of ``fleet.mower.area_names_changed``: all custom names of the mower after the
    change (area id to name; an area without an override is absent)."""

    mower_id: UUID
    account_id: UUID
    names: dict[str, str] = Field(default_factory=dict)


def is_placeholder_name(name: str | None) -> bool:
    """True for a name that says nothing: missing, blank or only a long number (a hash)."""
    cleaned = (name or "").strip()
    return not cleaned or bool(_HASH_NAME.match(cleaned))


def area_fallback_name(index: int, locale: str = "en") -> str:
    """``"Area N"`` / ``"Bereich N"`` for the 0-based ``index`` in the map's area order."""
    language = locale.replace("_", "-").split("-", 1)[0].lower()
    template = AREA_FALLBACK.get(language, AREA_FALLBACK["en"])
    return template.format(n=index + 1)


def area_display_name(
    area_id: str,
    areas: MowerMap | Sequence[MapArea] | None,
    overrides: Mapping[str, str] | None = None,
    *,
    locale: str = "en",
) -> str:
    """The name to show for ``area_id`` (see the module docstring for the rule).

    ``areas`` is the map (or its areas in map order). An area that is not on the map keeps its
    custom name, else its id: a schedule may still name an area the mower has since deleted.
    """
    custom = (overrides or {}).get(area_id, "").strip()
    if custom:
        return custom
    listed = areas.areas if isinstance(areas, MowerMap) else list(areas or [])
    for index, area in enumerate(listed):
        if area.id == area_id:
            if not is_placeholder_name(area.name):
                return (area.name or "").strip()
            return area_fallback_name(index, locale)
    return area_id


def area_display_names(
    areas: MowerMap | Sequence[MapArea] | None,
    overrides: Mapping[str, str] | None = None,
    *,
    locale: str = "en",
) -> dict[str, str]:
    """Display names of every area of the map, by id, in map order."""
    listed = areas.areas if isinstance(areas, MowerMap) else list(areas or [])
    return {a.id: area_display_name(a.id, listed, overrides, locale=locale) for a in listed}


def clean_area_name(name: str | None) -> str | None:
    """A custom name as Fleet stores it: without control and format characters
    (:func:`~mowdeck_contracts.v1.text.clean_text`), trimmed, inner whitespace collapsed; None
    for empty (which deletes the override). Raises ``ValueError`` above
    :data:`AREA_NAME_MAX_LENGTH`."""
    cleaned = " ".join(clean_text(name or "").split())
    if not cleaned:
        return None
    if len(cleaned) > AREA_NAME_MAX_LENGTH:
        msg = f"an area name has at most {AREA_NAME_MAX_LENGTH} characters"
        raise ValueError(msg)
    return cleaned
