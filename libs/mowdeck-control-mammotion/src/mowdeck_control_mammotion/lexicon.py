"""The Mammotion error lexicon (ADR 0067): every code of PyMammotion's table in plain words.

Registered through the entry point group ``mowdeck.error_lexicons`` under ``mammotion``
(``pyproject.toml``); ``mowdeck-control`` loads it at the first lookup. Built once from two
sources:

* **PyMammotion 0.10.9's bundled table** (``pymammotion/data/error_codes.csv``, the vendor's own
  export: about 660 codes, English for all, German for about two thirds). The vendor's
  "implication" is the title, its "solution" is split into steps. German is used only where the
  table has a German title and German steps (or no steps at all), so no entry mixes languages.
* **Curated entries** (:data:`CURATED`) for the codes MowDeck acts on: stuck, blocked, outside
  the area, wheel slip, lifted or tilted, battery and positioning. They replace the vendor's
  terse (and partly machine translated) words with a description, the likely cause and steps in
  English and German.

Categories always come from :func:`~mowdeck_control_mammotion.errors.category_of`, the same
function that fills ``MowerState.error_category``, so the lexicon and the state never disagree.
An entry is ``verified`` when its code is in the vendor's table; the codes PyMammotion read out
of the firmware (no vendor level, English words of PyMammotion's authors, e.g. ``11131``) are
marked unverified. Codes arrive with a sign from the firmware (``-1201``): the provider's
``normalise`` drops it.
"""

import re
from dataclasses import dataclass
from functools import cache
from typing import Any, Final

from mowdeck_contracts.v1 import (
    GENERIC_SEVERITY,
    ConnectionKind,
    ErrorLexicon,
    ErrorLexiconEntry,
    ErrorSeverity,
    ErrorText,
    MowerErrorCategory,
)
from mowdeck_control.error_lexicons import ErrorLexiconProvider

from mowdeck_control_mammotion import errors
from mowdeck_control_mammotion.port import bundled_error_codes

VENDOR: Final = "mammotion"
SOURCE: Final = (
    "PyMammotion 0.10.9 bundled error table (vendor export) with curated MowDeck texts for "
    "the codes MowDeck acts on"
)
SKIPPED_CODES: Final = frozenset({"0"})
"""Table rows that are no error ("Request succeeded")."""


@dataclass(frozen=True)
class Curated:
    """Hand-written words for one code, English and German."""

    en: ErrorText
    de: ErrorText
    severity: ErrorSeverity | None = None
    user_action: bool = True


def _t(title: str, description: str, cause: str | None, *steps: str) -> ErrorText:
    return ErrorText(title=title, description=description, cause=cause, steps=list(steps))


_STUCK = Curated(
    en=_t(
        "Stuck",
        "The mower cannot move on by itself and stopped its job.",
        "A hollow, a root, soft ground or something under the mower holds it.",
        "Look at the mower and remove what holds it.",
        "If you can, drive it free with the joystick in MowDeck or let AI assist propose a way.",
        "Resume the job once it stands on open lawn.",
    ),
    de=_t(
        "Steckt fest",
        "Der Mäher kommt nicht mehr von selbst weiter und hat seinen Auftrag angehalten.",
        "Eine Mulde, eine Wurzel, weicher Boden oder etwas unter dem Mäher hält ihn fest.",
        "Sieh nach dem Mäher und entferne, was ihn festhält.",
        "Wenn möglich, fahr ihn in MowDeck mit dem Joystick frei oder lass die KI-Hilfe einen Weg "
        "vorschlagen.",
        "Setze den Auftrag fort, sobald er auf freiem Rasen steht.",
    ),
)
_BLOCKED = Curated(
    en=_t(
        "The way is blocked",
        "An obstacle blocks the channel or the route the mower needs.",
        "Something stands on the way (furniture, a toy, a closed gate) or a no-go zone or "
        "virtual fence closes the channel.",
        "Clear the way in front of the mower.",
        "Check the channels, no-go zones and virtual fences on the map.",
        "Resume the job or send the mower back to its charging station.",
    ),
    de=_t(
        "Der Weg ist versperrt",
        "Ein Hindernis versperrt den Durchgang oder den Weg, den der Mäher braucht.",
        "Etwas steht im Weg (Möbel, Spielzeug, ein geschlossenes Tor) oder eine Sperrzone bzw. "
        "ein virtueller Zaun schließt den Durchgang.",
        "Räum den Weg vor dem Mäher frei.",
        "Prüfe Durchgänge, Sperrzonen und virtuelle Zäune auf der Karte.",
        "Setze den Auftrag fort oder schick den Mäher zurück zur Ladestation.",
    ),
)
_OUTSIDE = Curated(
    en=_t(
        "Outside the mowing area",
        "The mower stands outside every mowing area or inside a no-go zone, so it cannot start.",
        "It was carried or pushed there, or its position drifted near the edge of the map.",
        "Drive the mower into a mowing area or onto the charging station.",
        "Start the job again.",
        "If it happens often at the same place, check the map edges there.",
    ),
    de=_t(
        "Außerhalb des Mähbereichs",
        "Der Mäher steht außerhalb jedes Mähbereichs oder in einer Sperrzone und kann nicht "
        "starten.",
        "Er wurde dorthin getragen oder geschoben, oder seine Position ist am Kartenrand "
        "verrutscht.",
        "Fahr den Mäher in einen Mähbereich oder auf die Ladestation.",
        "Starte den Auftrag erneut.",
        "Passiert es oft an derselben Stelle, prüfe dort die Kartenränder.",
    ),
)
_SLIP = Curated(
    en=_t(
        "Wheels slipping",
        "The wheels turn but the mower does not move as it should.",
        "Wet grass, mud, a steep slope or dirty wheels.",
        "Check the ground: wet grass, mud or a slope that is too steep.",
        "Clean the wheels.",
        "Move the mower to firm ground and try again.",
    ),
    de=_t(
        "Räder drehen durch",
        "Die Räder drehen, aber der Mäher bewegt sich nicht wie erwartet.",
        "Nasses Gras, Matsch, ein zu steiler Hang oder verschmutzte Räder.",
        "Prüfe den Boden: nasses Gras, Matsch oder ein zu steiler Hang.",
        "Reinige die Räder.",
        "Stell den Mäher auf festen Boden und versuche es erneut.",
    ),
)
_LIFTED = Curated(
    en=_t(
        "Lifted",
        "The lift sensor stopped the blades and wheels.",
        "Someone lifted the mower, or it drove over something that raised its body.",
        "Check on the mower now: if nobody from your household lifted it, it may be a theft.",
        "Put it on flat ground.",
        "Unlock it on the mower (PIN) to continue.",
    ),
    de=_t(
        "Angehoben",
        "Der Hebesensor hat Messer und Räder gestoppt.",
        "Jemand hat den Mäher hochgehoben, oder er ist über etwas gefahren, das ihn angehoben hat.",
        "Sieh sofort nach dem Mäher: War es niemand aus deinem Haushalt, kann es ein Diebstahl "
        "sein.",
        "Stell ihn auf ebenen Boden.",
        "Entsperre ihn am Mäher (PIN), um weiterzumachen.",
    ),
    severity=ErrorSeverity.CRITICAL,
)
_TILTED = Curated(
    en=_t(
        "Tilted",
        "The tilt sensor stopped the mower: it stands too steep or was tipped.",
        "A slope that is too steep, a hole, or someone tipped the mower.",
        "Check on the mower and put it on flat ground.",
        "Unlock it on the mower to continue.",
        "If it happens on the same slope, keep the mower out of it with a no-go zone.",
    ),
    de=_t(
        "Gekippt",
        "Der Neigungssensor hat den Mäher gestoppt: Er steht zu schräg oder wurde gekippt.",
        "Ein zu steiler Hang, ein Loch, oder jemand hat den Mäher gekippt.",
        "Sieh nach dem Mäher und stell ihn auf ebenen Boden.",
        "Entsperre ihn am Mäher, um weiterzumachen.",
        "Passiert es am selben Hang, halte den Mäher mit einer Sperrzone davon fern.",
    ),
    severity=ErrorSeverity.CRITICAL,
)
_REAR_LIFT = Curated(
    en=_t(
        "Rear lifted while manoeuvring",
        "The rear lift sensor stopped the blades while the mower reversed or turned.",
        "The rear rose over an edge, a root or a bump, or someone lifted it.",
        "Check on the mower and put it on flat ground.",
        "Remove the edge or bump it backed onto, or keep it out with a no-go zone.",
        "Unlock it on the mower to continue.",
    ),
    de=_t(
        "Heck beim Rangieren angehoben",
        "Der hintere Hebesensor hat die Messer gestoppt, während der Mäher zurücksetzte oder "
        "wendete.",
        "Das Heck ist über eine Kante, eine Wurzel oder eine Bodenwelle gestiegen, oder jemand hat "
        "ihn angehoben.",
        "Sieh nach dem Mäher und stell ihn auf ebenen Boden.",
        "Entferne die Kante oder Bodenwelle oder halte ihn mit einer Sperrzone davon fern.",
        "Entsperre ihn am Mäher, um weiterzumachen.",
    ),
    severity=ErrorSeverity.CRITICAL,
)

CURATED: Final[dict[int, Curated]] = {
    1201: _STUCK,
    1421: Curated(
        en=_t(
            "Stuck on the way to the charging station",
            "The mower got stuck while returning to charge.",
            "An obstacle or soft ground on the return route.",
            "Remove obstacles on the return route.",
            "Free the mower and send it back to the charging station.",
            "If it keeps happening, contact Mammotion support.",
        ),
        de=_t(
            "Auf dem Weg zur Ladestation festgefahren",
            "Der Mäher ist auf dem Rückweg zum Laden stecken geblieben.",
            "Ein Hindernis oder weicher Boden auf dem Rückweg.",
            "Entferne Hindernisse auf dem Rückweg.",
            "Befreie den Mäher und schick ihn zurück zur Ladestation.",
            "Passiert es immer wieder, wende dich an den Mammotion-Support.",
        ),
    ),
    11112: _STUCK,
    2000701: Curated(
        en=_t(
            "Front wheels may be stuck",
            "The front wheels turn hard; the mower reports it as a notice.",
            "Dirt or grass between the front wheel bracket and the body.",
            "Clean between the front wheel bracket and the body and lubricate it.",
            "If nothing looks wrong, the notice can be ignored.",
        ),
        de=_t(
            "Vorderräder könnten blockieren",
            "Die Vorderräder drehen schwer; der Mäher meldet das als Hinweis.",
            "Schmutz oder Gras zwischen Vorderradhalterung und Gehäuse.",
            "Reinige den Bereich zwischen Vorderradhalterung und Gehäuse und schmiere ihn.",
            "Ist nichts zu sehen, kann der Hinweis ignoriert werden.",
        ),
        severity=ErrorSeverity.INFO,
    ),
    1206: _BLOCKED,
    1214: _BLOCKED,
    11113: _BLOCKED,
    11121: _BLOCKED,
    11122: _BLOCKED,
    100: _OUTSIDE,
    1100: _OUTSIDE,
    1145: _OUTSIDE,
    1411: _OUTSIDE,
    1422: Curated(
        en=_t(
            "Could not return to the charging station",
            "The return to the charging station failed and the mower is outside the mowing area.",
            "Its position drifted, or it was pushed out of the area while returning.",
            "Drive the mower into a mowing area or next to the charging station.",
            "Send it back to the charging station again.",
        ),
        de=_t(
            "Rückkehr zur Ladestation fehlgeschlagen",
            "Die Rückkehr zur Ladestation ist gescheitert und der Mäher steht außerhalb des "
            "Mähbereichs.",
            "Seine Position ist verrutscht, oder er wurde beim Zurückfahren aus dem Bereich "
            "geschoben.",
            "Fahr den Mäher in einen Mähbereich oder neben die Ladestation.",
            "Schick ihn erneut zur Ladestation.",
        ),
    ),
    1032: _SLIP,
    1218: _SLIP,
    11132: _SLIP,
    11134: _SLIP,
    1004: _TILTED,
    1030: Curated(
        en=_t(
            "Front wheels lifted or tilted",
            "The mower's front wheels lost the ground, so it stopped.",
            "It climbed onto an obstacle or edge, or someone lifted its front.",
            "Check on the mower and put it on flat ground.",
            "Remove the obstacle it climbed onto.",
            "Start again.",
        ),
        de=_t(
            "Vorderräder angehoben oder gekippt",
            "Die Vorderräder des Mähers haben den Boden verloren, deshalb hat er angehalten.",
            "Er ist auf ein Hindernis oder eine Kante gefahren, oder jemand hat ihn vorne "
            "angehoben.",
            "Sieh nach dem Mäher und stell ihn auf ebenen Boden.",
            "Entferne das Hindernis, auf das er gefahren ist.",
            "Starte erneut.",
        ),
        severity=ErrorSeverity.CRITICAL,
    ),
    2502: _TILTED,
    2801: _LIFTED,
    11131: _REAR_LIFT,
    11133: _REAR_LIFT,
    1005: Curated(
        en=_t(
            "Battery low",
            "The battery is too low to go on; the mower charges first.",
            None,
            "Nothing to do: the mower resumes the unfinished job once it is almost fully charged.",
        ),
        de=_t(
            "Akku fast leer",
            "Der Akku ist zu leer zum Weitermachen; der Mäher lädt zuerst.",
            None,
            "Nichts zu tun: Der Mäher setzt den Auftrag fort, sobald er fast voll geladen ist.",
        ),
        severity=ErrorSeverity.INFO,
        user_action=False,
    ),
    1300: Curated(
        en=_t(
            "Poor positioning",
            "The mower's position is not precise enough to work.",
            "Weak satellite reception (trees, walls) or no corrections from the RTK base station.",
            "Wait a few minutes: the position usually recovers by itself.",
            "Check the RTK base station: power, antenna, clear view of the sky.",
        ),
        de=_t(
            "Schlechte Positionierung",
            "Die Position des Mähers ist nicht genau genug zum Arbeiten.",
            "Schwacher Satellitenempfang (Bäume, Mauern) oder keine Korrekturdaten von der "
            "RTK-Basisstation.",
            "Warte ein paar Minuten: Die Position kommt meist von selbst zurück.",
            "Prüfe die RTK-Basisstation: Strom, Antenne, freie Sicht zum Himmel.",
        ),
        severity=ErrorSeverity.WARNING,
        user_action=False,
    ),
}
"""Hand-written texts for the codes MowDeck acts on (the recoverable codes of
:data:`~mowdeck_control_mammotion.errors.RECOVERABLE_CODES`, the lift and tilt codes of
:data:`~mowdeck_control_mammotion.errors.LIFT_TILT_CODES`, battery and positioning)."""

_INVISIBLE = re.compile(r"[\u200b-\u200f\ufeff]")
_PLACEHOLDER = re.compile(r"\s*\(\s*%s\s*\)|%s")
_NUMBERED = re.compile(r"(?:^|(?<=[.:!?;]))\s*\d{1,2}\.\s*(?=\S)")
_SENTENCE = re.compile(r"(?<=[.!?。])\s+(?=[A-ZÄÖÜ])|(?<=[a-z][.!?])(?=[A-ZÄÖÜ])")
MAX_STEPS: Final = 12


def clean(text: str) -> str:
    """Vendor text without invisible characters, ``%s`` placeholders and doubled spaces."""
    text = _PLACEHOLDER.sub("", _INVISIBLE.sub("", text))
    return " ".join(text.split())


def split_steps(solution: str) -> list[str]:
    """The vendor's solution as steps: numbered items (``1. ... 2. ...``) and sentences."""
    text = _INVISIBLE.sub("", solution).replace("\r", "\n")
    text = _NUMBERED.sub("\n", text)
    steps: list[str] = []
    for chunk in text.split("\n"):
        for sentence in _SENTENCE.split(chunk):
            step = clean(sentence)
            if step and step not in steps:
                steps.append(step[:500])
    return steps[:MAX_STEPS]


def _texts(row: Any) -> dict[str, ErrorText]:
    en_title = clean(row.en_implication) or clean(row.description)
    texts = {"en": ErrorText(title=en_title[:200], steps=split_steps(row.en_solution))}
    de_title = clean(row.de_implication)
    if de_title and (row.de_solution.strip() or not row.en_solution.strip()):
        texts["de"] = ErrorText(title=de_title[:200], steps=split_steps(row.de_solution))
    return texts


def _entry(code: int, row: Any | None) -> ErrorLexiconEntry:
    category = MowerErrorCategory(errors.category_of(code))
    curated = CURATED.get(code)
    verified = row is not None and bool(row.level.strip())
    if curated is not None:
        return ErrorLexiconEntry(
            code=str(code),
            aliases=[f"-{code}"],
            category=category,
            severity=curated.severity or GENERIC_SEVERITY.get(category, ErrorSeverity.WARNING),
            user_action=curated.user_action,
            texts={"en": curated.en, "de": curated.de},
            verified=verified,
        )
    assert row is not None  # noqa: S101 - only curated codes may be missing from the table
    return ErrorLexiconEntry(
        code=str(code),
        aliases=[f"-{code}"],
        category=category,
        severity=GENERIC_SEVERITY.get(category, ErrorSeverity.WARNING),
        user_action=category is not MowerErrorCategory.RAIN,
        texts=_texts(row),
        verified=verified,
    )


def build_lexicon() -> ErrorLexicon:
    """The lexicon from PyMammotion's bundled table and :data:`CURATED` (no I/O, ~50 ms)."""
    table = bundled_error_codes()
    codes: dict[int, Any] = {}
    for key, row in table.items():
        number = errors.normalise_code(key)
        if number is None or key in SKIPPED_CODES or not clean(row.en_implication):
            continue
        codes[number] = row
    for number in CURATED:
        codes.setdefault(number, None)
    return ErrorLexicon(
        vendor=VENDOR,
        vendor_name="Mammotion",
        connection_kinds=[ConnectionKind.CLOUD, ConnectionKind.BLE_BRIDGE],
        source=SOURCE,
        entries=[_entry(code, codes[code]) for code in sorted(codes)],
    )


def normalise(code: str) -> str:
    """``-1201`` (as the firmware reports it) gives ``1201``; other text stays as it is."""
    number = errors.normalise_code(code)
    return str(number) if number is not None else code.strip()


@cache
def mammotion_lexicon() -> ErrorLexiconProvider:
    """The provider of the entry point ``mowdeck.error_lexicons`` ``mammotion`` (built once)."""
    return ErrorLexiconProvider(build_lexicon(), normalise=normalise)


__all__ = [
    "CURATED",
    "SOURCE",
    "VENDOR",
    "Curated",
    "build_lexicon",
    "clean",
    "mammotion_lexicon",
    "normalise",
    "split_steps",
]
