"""A small deterministic phrase parser: the assistant without any AI (ADR 0075).

Used in demo mode (no external AI may be called, ADR 0068), when the operator chooses
``MOWDECK_ASSISTANT_INTERPRETER=phrases`` and in the end-to-end tests. It understands a few
German and English phrases ("mäh morgen früh nur hinten", "schick ihn zur Station",
"pausier bis 15 Uhr", "mow the back lawn now", "resume", ...) and answers in exactly the shape
a model answers, so its output goes through the same validator.
"""

import re
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from assistant.domain.model import ActionKind, Catalog, Note

_WORD = re.compile(r"[a-zäöüß0-9]+")

_DRIVE = re.compile(
    r"\b(fahr|fahre|lenk|lenke|steuer|steuere|drive|steer|joystick|links|rechts|left|right|"
    r"vorwärts|rückwärts|forward|backward)\b"
)
_SETTINGS = re.compile(
    r"\b(schnitthöhe|höhe|messer|geschwindigkeit|einstellung|einstellen|blade|height|speed|"
    r"setting|settings)\b"
)
_DOCK = re.compile(
    r"\b(station|ladestation|dock|docken|nach hause|heim|home|base|zurück|back to base)\b"
)
_PAUSE = re.compile(r"\b(pause|pausier|pausiere|pausieren|anhalten|halt an|stop|stopp)\b")
_RESUME = re.compile(r"\b(weiter|weitermachen|fortsetzen|setz fort|resume|continue)\b")
_MOW = re.compile(r"\b(mäh|mähe|mähen|mow|mowing|start|starte|starten|schneid)\w*")
_UNTIL = re.compile(r"\b(bis|until|till)\b")
_NOW = re.compile(r"\b(jetzt|sofort|gleich|now|right away)\b")
_TOMORROW = re.compile(r"\b(morgen|tomorrow)\b")
_TODAY = re.compile(r"\b(heute|today|tonight)\b")
_AT = re.compile(r"\b(?:um|at|ab|from)\s+(\d{1,2})(?:[:.](\d{2}))?\s*(uhr|h|am|pm)?\b")
_BARE_TIME = re.compile(r"\b(\d{1,2})[:.](\d{2})\b|\b(\d{1,2})\s*(uhr|am|pm)\b")
_PARTS = (
    (re.compile(r"\b(früh|morgens|vormittag|morning)\b"), 8),
    (re.compile(r"\b(mittag|mittags|noon|midday)\b"), 12),
    (re.compile(r"\b(nachmittag|nachmittags|afternoon)\b"), 15),
    (re.compile(r"\b(abend|abends|evening|tonight)\b"), 18),
)
_SYNONYMS = {
    "hinten": "back",
    "hinter": "back",
    "hinteren": "back",
    "hintere": "back",
    "rear": "back",
    "garten": "back",
    "vorne": "front",
    "vorn": "front",
    "vorderen": "front",
    "vordere": "front",
    "seite": "side",
    "seitlich": "side",
    "wiese": "meadow",
}
_GENERIC = {"lawn", "rasen", "area", "zone", "fläche", "the", "den", "die", "das", "der", "nur"}


def _tokens(text: str) -> set[str]:
    words = set(_WORD.findall(text.lower()))
    return {_SYNONYMS.get(w, w) for w in words} - _GENERIC


def _areas(text: str, catalog: Catalog) -> list[str]:
    """Ids of the areas whose name or id shares a word with the request (synonyms mapped)."""
    wanted = _tokens(text)
    found: list[str] = []
    for area in catalog.areas:
        words = _tokens(area.name) | _tokens(area.id.replace("-", " ").replace("_", " "))
        if words & wanted:
            found.append(area.id)
    return found


def _clock(text: str) -> tuple[int, int] | None:
    match = _AT.search(text)
    if match:
        hour, minute, suffix = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    else:
        bare = _BARE_TIME.search(text)
        if bare is None:
            for pattern, part_hour in _PARTS:
                if pattern.search(text):
                    return part_hour, 0
            return None
        if bare.group(1) is not None:
            hour, minute, suffix = int(bare.group(1)), int(bare.group(2)), None
        else:
            hour, minute, suffix = int(bare.group(3)), 0, bare.group(4)
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _day(text: str, today: date) -> date | None:
    if _TOMORROW.search(text):
        return today + timedelta(days=1)
    if _TODAY.search(text):
        return today
    return None


def _action(kind: ActionKind, areas: list[str] | None = None) -> dict[str, Any]:
    return {"kind": kind.value, "area_ids": areas or [], "start_at": None, "duration_minutes": None}


def parse(text: str, catalog: Catalog) -> dict[str, Any]:
    """The answer for ``text``: ``{"actions": [...], "notes": [...]}`` like a model's."""
    lowered = " ".join(text.lower().split())
    if _DRIVE.search(lowered) and not _MOW.search(lowered):
        return {"actions": [], "notes": [Note.MANUAL_DRIVE.value]}
    if _SETTINGS.search(lowered):
        return {"actions": [], "notes": [Note.SETTINGS.value]}
    if _PAUSE.search(lowered):
        notes = [Note.TIMED_RESUME.value] if _UNTIL.search(lowered) else []
        return {"actions": [_action(ActionKind.PAUSE)], "notes": notes}
    if _RESUME.search(lowered):
        return {"actions": [_action(ActionKind.RESUME)], "notes": []}
    if _DOCK.search(lowered) and not _MOW.search(lowered):
        return {"actions": [_action(ActionKind.DOCK)], "notes": []}
    if _MOW.search(lowered):
        areas = _areas(lowered, catalog)
        local_now = catalog.now.astimezone(ZoneInfo(catalog.timezone))
        clock = _clock(lowered)
        day = _day(lowered, local_now.date())
        if (clock is None and day is None) or _NOW.search(lowered):
            return {"actions": [_action(ActionKind.START_NOW, areas)], "notes": []}
        hour, minute = clock if clock is not None else (8, 0)
        on = day or local_now.date()
        later = _action(ActionKind.START_LATER, areas)
        later["start_at"] = f"{on.isoformat()}T{hour:02d}:{minute:02d}"
        return {"actions": [later], "notes": []}
    return {"actions": [], "notes": [Note.UNCLEAR.value]}
