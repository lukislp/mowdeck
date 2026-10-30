"""The Worx error lexicon (ADR 0067): every error code of the cloud in plain words.

Registered through the entry point group ``mowdeck.error_lexicons`` under ``worx``
(``pyproject.toml``); ``mowdeck-control`` builds it at the first lookup. Worx Landroid, Kress and
LandXcape mowers report the same codes (``dat.le``), so one lexicon serves all three brands.

Sources: **pyworxcloud 6.4.2**'s tables (``ERROR_TO_DESCRIPTION`` and ``STATE_TO_DESCRIPTION``
in ``pyworxcloud/utils/state.py``), copied as :data:`ENGLISH_NAMES` and :data:`STATUS_NAMES`
so that loading the lexicon stays cheap and never imports the vendor library (a test keeps the
copies equal). The vendor documents no code table; pyworxcloud's names come from reverse
engineering the app, so **every entry is** ``verified: false``. MowDeck adds per code the
neutral category (:data:`CATEGORIES`, which also fills ``MowerState.error_category``), the
severity, whether a person has to act, a German title and a description and steps per
category in English and German; :data:`CURATED` has words of their own for the codes MowDeck
acts on (lifted, upside down, trapped, outside the wire, rain delay, battery, charging).

The lexicon lists no connection kinds: Worx mowers connect as ``cloud`` like other vendors'
cloud mowers, so it is found by its vendor key (``worx``), not by the connection kind.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Final

from mowdeck_contracts.v1 import (
    ErrorLexicon,
    ErrorLexiconEntry,
    ErrorSeverity,
    ErrorText,
    MowerErrorCategory,
)
from mowdeck_control.error_lexicons import ErrorLexiconProvider

from mowdeck_control_worx.descriptor import PROVIDER

VENDOR: Final = PROVIDER
SOURCE: Final = (
    "pyworxcloud 6.4.2 error and status tables (reverse engineered, not confirmed by the "
    "vendor) with MowDeck categories, German titles and steps"
)

C = MowerErrorCategory

ENGLISH_NAMES: Final[Mapping[int, str]] = {
    -1: "unknown",
    0: "no error",
    1: "trapped",
    2: "lifted",
    3: "wire missing",
    4: "outside wire",
    5: "rain delay",
    6: "close door to mow",
    7: "close door to go home",
    8: "blade motor blocked",
    9: "wheel motor blocked",
    10: "trapped timeout",
    11: "upside down",
    12: "battery low",
    13: "reverse wire",
    14: "charge error",
    15: "timeout finding home",
    16: "locked",
    17: "battery temperature error",
    18: "dummy model",
    19: "battery trunk open timeout",
    20: "wire sync",
    21: "msg num",
    100: "charging station docking error",
    101: "hbi error",
    102: "ota error",
    103: "map error",
    104: "excessive slope",
    105: "unreachable zone",
    106: "unreachable charging station",
    108: "insufficient sensor data",
    109: "training start disallowed",
    110: "camera error",
    111: "mapping exploration required",
    112: "mapping exploration failed",
    113: "rfid reader error",
    114: "headlight error",
    115: "missing charging station",
    116: "blade height adjustment blocked",
}
"""pyworxcloud's error names by code (``dat.le``), unchanged; -1 and 0 are no errors."""

STATUS_NAMES: Final[Mapping[int, str]] = {
    -1: "unknown",
    0: "idle",
    1: "home",
    2: "start sequence",
    3: "leaving home",
    4: "follow wire",
    5: "searching home",
    6: "searching wire",
    7: "mowing",
    8: "mowing",
    9: "trapped",
    10: "blade blocked",
    11: "debug",
    12: "remote control",
    13: "digital fence escape",
    30: "going home",
    31: "zoning",
    32: "cutting edge",
    33: "searching area",
    34: "pause",
    103: "searching zone",
    104: "searching home",
    110: "border crossing",
    111: "exploring lawn",
}
"""pyworxcloud's status names by code (``dat.ls``), unchanged; the mapping reads them."""


def _codes(category: MowerErrorCategory, codes: Iterable[int]) -> dict[int, MowerErrorCategory]:
    return dict.fromkeys(codes, category)


CATEGORIES: Final[Mapping[int, MowerErrorCategory]] = {
    **_codes(C.TRAPPED, (1, 9, 10)),
    **_codes(C.LIFT_TILT, (2, 11)),
    **_codes(C.POSITIONING, (3, 13, 20, 103, 108, 111, 112)),
    **_codes(C.OUTSIDE, (4,)),
    **_codes(C.RAIN, (5,)),
    **_codes(C.BLADE_MOTOR, (8, 116)),
    **_codes(C.BATTERY, (12, 17, 19)),
    **_codes(C.DOCKING, (14, 15, 100, 106, 115)),
    **_codes(C.FIRMWARE, (102,)),
    **_codes(C.OBSTACLE, (105,)),
}
"""Worx codes by neutral category; every other code is ``other``. A blocked wheel motor (9)
counts as trapped: something holds the mower and a person frees it. The excessive slope (104)
is ``other``, not ``lift_tilt``: the mower stops before a slope, nobody lifted it, so it must not
raise the theft alarm."""

LIFT_TILT_CODES: Final = frozenset(c for c, cat in CATEGORIES.items() if cat is C.LIFT_TILT)
"""Lifted (2) and upside down (11): the theft alarm's codes (ADR 0066)."""

NO_ERROR_CODES: Final = frozenset({-1, 0})
_INFO_CODES: Final = frozenset({5})
"""The rain delay: the mower waits by itself; nothing to do."""
_DEFECTS: Final = frozenset({3, 13, 14, 18, 21, 101, 102, 103, 110, 113, 114})
"""Faults the mower cannot clear by itself: a repair, the installation or the dealer."""


def severity_of(code: int) -> ErrorSeverity:
    """Lift and upside down critical, the rain delay info, defects errors, the rest warnings."""
    if code in LIFT_TILT_CODES:
        return ErrorSeverity.CRITICAL
    if code in _INFO_CODES:
        return ErrorSeverity.INFO
    if code in _DEFECTS:
        return ErrorSeverity.ERROR
    return ErrorSeverity.WARNING


def category_of(code: int) -> MowerErrorCategory:
    """The neutral category of ``code`` (``other`` when not listed)."""
    return CATEGORIES.get(code, C.OTHER)


def is_error(code: int) -> bool:
    """``code`` stops the mower: everything except "no error" and the rain delay (a mower in
    its rain delay waits at the station and is not broken)."""
    return code not in NO_ERROR_CODES and code not in _INFO_CODES


GERMAN_TITLES: Final[Mapping[int, str]] = {
    1: "Eingeklemmt",
    2: "Angehoben",
    3: "Begrenzungskabel fehlt",
    4: "Außerhalb des Begrenzungskabels",
    5: "Regenverzögerung",
    6: "Klappe schließen zum Mähen",
    7: "Klappe schließen zur Heimfahrt",
    8: "Messermotor blockiert",
    9: "Radmotor blockiert",
    10: "Eingeklemmt (Zeitüberschreitung)",
    11: "Liegt auf dem Kopf",
    12: "Akku schwach",
    13: "Begrenzungskabel vertauscht",
    14: "Ladefehler",
    15: "Ladestation nicht gefunden (Zeitüberschreitung)",
    16: "Gesperrt",
    17: "Akkutemperatur außerhalb des Bereichs",
    18: "Unbekanntes Modell",
    19: "Akkufach zu lange offen",
    20: "Kabelsignal nicht synchron",
    21: "Nachrichtenfehler",
    100: "Andockfehler an der Ladestation",
    101: "HBI-Fehler",
    102: "Fehler beim Firmware-Update",
    103: "Kartenfehler",
    104: "Zu steiler Hang",
    105: "Zone nicht erreichbar",
    106: "Ladestation nicht erreichbar",
    108: "Zu wenige Sensordaten",
    109: "Zonentraining nicht erlaubt",
    110: "Kamerafehler",
    111: "Erkundung des Rasens nötig",
    112: "Erkundung des Rasens fehlgeschlagen",
    113: "Fehler des RFID-Lesers",
    114: "Fehler des Scheinwerfers",
    115: "Ladestation fehlt",
    116: "Schnitthöhenverstellung blockiert",
}
"""German titles of every error code MowDeck explains."""

ENGLISH_TITLES: Final[Mapping[int, str]] = {
    code: name[:1].upper() + name[1:]
    for code, name in {
        **ENGLISH_NAMES,
        8: "blade motor blocked",
        16: "locked (PIN)",
        19: "battery compartment open too long",
        21: "message error",
        101: "HBI error",
        102: "firmware update error",
        113: "RFID reader error",
    }.items()
    if code not in NO_ERROR_CODES
}
"""English titles: pyworxcloud's names with a capital letter (a few spelled out)."""


@dataclass(frozen=True, slots=True)
class _Advice:
    en: tuple[str, tuple[str, ...]]
    de: tuple[str, tuple[str, ...]]


_ADVICE: Final[Mapping[MowerErrorCategory, _Advice]] = {
    C.TRAPPED: _Advice(
        en=("Something holds the mower; it stopped.", ("Free the mower.", "Start it again.")),
        de=(
            "Etwas hält den Mäher fest; er hat angehalten.",
            ("Befreie den Mäher.", "Starte ihn erneut."),
        ),
    ),
    C.POSITIONING: _Advice(
        en=(
            "The mower cannot follow its boundary (wire signal or map).",
            (
                "Check that the charging station has power and its light is on.",
                "Check the boundary wire for cuts and loose connectors.",
                "Start the mower again.",
            ),
        ),
        de=(
            "Der Mäher kann seiner Begrenzung nicht folgen (Kabelsignal oder Karte).",
            (
                "Prüfe, ob die Ladestation Strom hat und ihre Lampe leuchtet.",
                "Prüfe das Begrenzungskabel auf Schnitte und lose Verbinder.",
                "Starte den Mäher erneut.",
            ),
        ),
    ),
    C.BLADE_MOTOR: _Advice(
        en=(
            "The cutting system is blocked.",
            (
                "Switch the mower off and remove grass, twigs or stones from the blade disc.",
                "Start it again; if it stays, contact your dealer.",
            ),
        ),
        de=(
            "Das Schneidsystem ist blockiert.",
            (
                "Schalte den Mäher aus und entferne Gras, Zweige oder Steine von der "
                "Messerscheibe.",
                "Starte ihn erneut; bleibt der Fehler, wende dich an deinen Händler.",
            ),
        ),
    ),
    C.BATTERY: _Advice(
        en=(
            "The battery limits or stops the mower.",
            (
                "Put the mower into its charging station.",
                "If it comes back, contact your dealer about the battery.",
            ),
        ),
        de=(
            "Der Akku schränkt den Mäher ein oder stoppt ihn.",
            (
                "Stell den Mäher in seine Ladestation.",
                "Kommt es wieder, sprich deinen Händler auf den Akku an.",
            ),
        ),
    ),
    C.DOCKING: _Advice(
        en=(
            "The charging station or the charging reports a problem.",
            (
                "Check that the charging station has power and nothing blocks it.",
                "Clean the charging contacts and put the mower into the station.",
            ),
        ),
        de=(
            "Die Ladestation oder das Laden meldet ein Problem.",
            (
                "Prüfe, ob die Ladestation Strom hat und nichts sie blockiert.",
                "Reinige die Ladekontakte und stell den Mäher in die Station.",
            ),
        ),
    ),
    C.FIRMWARE: _Advice(
        en=(
            "The mower's software update did not complete.",
            ("Restart the mower and try the update again in the app.",),
        ),
        de=(
            "Das Software-Update des Mähers ist nicht fertig geworden.",
            ("Starte den Mäher neu und versuche das Update in der App erneut.",),
        ),
    ),
    C.OBSTACLE: _Advice(
        en=(
            "The mower cannot reach where it should go.",
            ("Clear the way to the zone or the station.", "Start the mower again."),
        ),
        de=(
            "Der Mäher kommt nicht dorthin, wo er hin soll.",
            ("Räum den Weg zur Zone oder zur Station frei.", "Starte den Mäher erneut."),
        ),
    ),
    C.OTHER: _Advice(
        en=(
            "The mower reports a problem.",
            (
                "Check the mower and its display.",
                "Restart it; if it stays, contact your dealer.",
            ),
        ),
        de=(
            "Der Mäher meldet ein Problem.",
            (
                "Prüfe den Mäher und seine Anzeige.",
                "Starte ihn neu; bleibt der Fehler, wende dich an deinen Händler.",
            ),
        ),
    ),
}


def _t(title: str, description: str, cause: str | None, *steps: str) -> ErrorText:
    return ErrorText(title=title, description=description, cause=cause, steps=list(steps))


_LIFTED_EN = (
    "The lift sensor stopped the blades and the wheels.",
    "Someone lifted the mower, or it is caught on something that raises it.",
    "Check on the mower now: MowDeck raised the theft alarm.",
    "Put it down on level lawn and confirm on the mower.",
)
_LIFTED_DE = (
    "Der Hebesensor hat Messer und Räder gestoppt.",
    "Jemand hat den Mäher hochgehoben, oder er hängt an etwas, das ihn anhebt.",
    "Sieh sofort nach dem Mäher: MowDeck hat den Diebstahlalarm ausgelöst.",
    "Stell ihn auf ebenen Rasen und bestätige am Mäher.",
)

CURATED: Final[Mapping[int, tuple[ErrorText, ErrorText]]] = {
    1: (
        _t(
            "Trapped",
            "The mower cannot get out of where it is and stopped.",
            "A narrow passage, a hollow, a root or something under the mower holds it.",
            "Free the mower and remove what trapped it.",
            "Start it again from the mower or MowDeck.",
            "If it happens at the same place, move the boundary wire there.",
        ),
        _t(
            "Eingeklemmt",
            "Der Mäher kommt von seiner Stelle nicht mehr weg und hat angehalten.",
            "Eine enge Stelle, eine Mulde, eine Wurzel oder etwas unter dem Mäher hält ihn fest.",
            "Befreie den Mäher und entferne, was ihn festhält.",
            "Starte ihn am Mäher oder in MowDeck neu.",
            "Passiert es immer an derselben Stelle, verlege dort das Begrenzungskabel.",
        ),
    ),
    2: (_t("Lifted", *_LIFTED_EN), _t("Angehoben", *_LIFTED_DE)),
    11: (
        _t(
            "Upside down",
            "The mower lies on its back; the blades and the wheels stopped.",
            "It tipped over on a slope or an edge, or someone turned it over.",
            "Check on the mower now: MowDeck raised the theft alarm.",
            "Turn it over onto level lawn, look for damage and confirm on the mower.",
        ),
        _t(
            "Liegt auf dem Kopf",
            "Der Mäher liegt auf dem Rücken; Messer und Räder sind gestoppt.",
            "Er ist an einem Hang oder einer Kante gekippt, oder jemand hat ihn umgedreht.",
            "Sieh sofort nach dem Mäher: MowDeck hat den Diebstahlalarm ausgelöst.",
            "Dreh ihn auf ebenen Rasen um, prüfe ihn auf Schäden und bestätige am Mäher.",
        ),
    ),
    4: (
        _t(
            "Outside the boundary wire",
            "The mower is outside its boundary wire and cannot start.",
            "It was carried there, or the wire signal is wrong at that place.",
            "Carry the mower back inside the boundary wire.",
            "Start it again.",
        ),
        _t(
            "Außerhalb des Begrenzungskabels",
            "Der Mäher steht außerhalb seines Begrenzungskabels und kann nicht starten.",
            "Er wurde dorthin getragen, oder das Kabelsignal stimmt dort nicht.",
            "Trag den Mäher zurück innerhalb des Begrenzungskabels.",
            "Starte ihn erneut.",
        ),
    ),
    5: (
        _t(
            "Rain delay",
            "The rain sensor got wet; the mower waits in its station until the rain delay is over.",
            "It rained, or the sensor is wet from dew or watering.",
        ),
        _t(
            "Regenverzögerung",
            "Der Regensensor ist nass geworden; der Mäher wartet in seiner Station, bis die "
            "Regenverzögerung vorbei ist.",
            "Es hat geregnet, oder der Sensor ist von Tau oder Gießwasser nass.",
        ),
    ),
    12: (
        _t(
            "Battery low",
            "The battery is too low and the mower stopped.",
            "It could not reach the charging station in time.",
            "Carry the mower into its charging station.",
            "If it happens often, check that it finds the station (wire signal, slope).",
        ),
        _t(
            "Akku schwach",
            "Der Akku ist zu schwach und der Mäher hat angehalten.",
            "Er hat die Ladestation nicht rechtzeitig erreicht.",
            "Trag den Mäher in seine Ladestation.",
            "Passiert es oft, prüfe, ob er die Station findet (Kabelsignal, Steigung).",
        ),
    ),
    14: (
        _t(
            "Charging error",
            "The mower sits in its station but does not charge.",
            "Dirty charging contacts, a bad contact or a faulty power supply.",
            "Clean the charging contacts of the mower and the station.",
            "Check the power supply; if it stays, contact your dealer.",
        ),
        _t(
            "Ladefehler",
            "Der Mäher steht in seiner Station, lädt aber nicht.",
            "Verschmutzte Ladekontakte, ein Wackelkontakt oder ein defektes Netzteil.",
            "Reinige die Ladekontakte am Mäher und an der Station.",
            "Prüfe das Netzteil; bleibt der Fehler, wende dich an deinen Händler.",
        ),
    ),
    15: (
        _t(
            "Charging station not found",
            "The mower looked for its charging station for too long and stopped.",
            "The wire signal is weak, the way home is blocked or the station moved.",
            "Carry the mower into its charging station.",
            "Clear the way along the boundary wire to the station.",
        ),
        _t(
            "Ladestation nicht gefunden",
            "Der Mäher hat zu lange nach seiner Ladestation gesucht und angehalten.",
            "Das Kabelsignal ist schwach, der Heimweg versperrt oder die Station verschoben.",
            "Trag den Mäher in seine Ladestation.",
            "Räum den Weg entlang des Begrenzungskabels zur Station frei.",
        ),
    ),
}
"""Hand-written words for the codes MowDeck acts on, English and German."""

_INFO_ADVICE: Final = _Advice(
    en=("The mower waits by itself; nothing to do.", ()),
    de=("Der Mäher wartet von selbst; nichts zu tun.", ()),
)


def _entry(code: int) -> ErrorLexiconEntry:
    category, severity = category_of(code), severity_of(code)
    curated = CURATED.get(code)
    if curated is not None:
        texts = {"en": curated[0], "de": curated[1]}
    else:
        advice = _INFO_ADVICE if severity is ErrorSeverity.INFO else _ADVICE[category]
        texts = {
            "en": ErrorText(
                title=ENGLISH_TITLES[code], description=advice.en[0], steps=list(advice.en[1])
            ),
            "de": ErrorText(
                title=GERMAN_TITLES[code], description=advice.de[0], steps=list(advice.de[1])
            ),
        }
    return ErrorLexiconEntry(
        code=str(code),
        category=category,
        severity=severity,
        user_action=severity is not ErrorSeverity.INFO,
        texts=texts,
        verified=False,
    )


def build_lexicon() -> ErrorLexicon:
    """The lexicon from pyworxcloud's table (-1 and 0 are no errors and left out)."""
    entries = [_entry(code) for code in sorted(ENGLISH_TITLES)]
    return ErrorLexicon(
        vendor=VENDOR,
        vendor_name="Worx",
        connection_kinds=[],
        source=SOURCE,
        entries=entries,
    )


def normalise(raw: str) -> str:
    """A raw code in the lexicon's spelling (``"002"`` and ``" 2 "`` give ``"2"``)."""
    text = raw.strip()
    return str(int(text)) if text.isdigit() else text


@cache
def worx_lexicon() -> ErrorLexiconProvider:
    """The entry point object (``mowdeck.error_lexicons``: ``worx``), built once."""
    return ErrorLexiconProvider(build_lexicon(), normalise=normalise)
