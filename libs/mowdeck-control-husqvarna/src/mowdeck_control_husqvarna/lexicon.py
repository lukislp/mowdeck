"""The Husqvarna error lexicon (ADR 0067): every code of aioautomower's table in plain words.

Registered through the entry point group ``mowdeck.error_lexicons`` under ``husqvarna``
(``pyproject.toml``); ``mowdeck-control`` builds it at the first lookup. Sources:

* **aioautomower 3.1.0's** ``ERRORCODES`` table (the English names the Automower Connect API
  documents for its error codes), copied as :data:`ENGLISH_TITLES` so that loading the lexicon
  stays cheap and does not import the vendor library: the English title of every entry.
* **MowDeck** adds per code the neutral category (:data:`CATEGORIES`, which also fills
  ``MowerState.error_category``), the severity, whether a person has to act, a German title
  (:data:`GERMAN_TITLES`), and a description and steps per category in English and German;
  :data:`CURATED` replaces them with words of their own for the codes MowDeck acts on (lifted,
  tilted, trapped, outside, theft alarms, battery, charging station, positioning).

Every entry is ``verified``: the code meanings come from the vendor's API documentation as
aioautomower carries it; the steps are MowDeck's general advice. The lexicon lists no
connection kinds: Husqvarna mowers connect as ``cloud`` like other vendors' cloud mowers, so it
is found by its vendor key (``husqvarna``), not by the connection kind.
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

from mowdeck_control_husqvarna.descriptor import PROVIDER

VENDOR: Final = PROVIDER
SOURCE: Final = (
    "aioautomower 3.1.0 error code table (names of the Automower Connect API) with MowDeck "
    "categories, German titles and steps"
)

C = MowerErrorCategory


def _codes(category: MowerErrorCategory, codes: Iterable[int]) -> dict[int, MowerErrorCategory]:
    return dict.fromkeys(codes, category)


CATEGORIES: Final[Mapping[int, MowerErrorCategory]] = {
    **_codes(C.TRAPPED, (9, 13, 55)),
    **_codes(C.WHEEL_SLIP, (78,)),
    **_codes(C.OUTSIDE, (1, 74, 92, 136)),
    **_codes(C.LIFT_TILT, (10, 14, 15, 33, 71, 72, 131)),
    **_codes(C.COLLISION, (110, 148)),
    **_codes(C.OBSTACLE, (123, 124, 154)),
    **_codes(
        C.BATTERY,
        (11, 12, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 79, 114, 125, 126, 127),
    ),
    **_codes(
        C.BLADE_MOTOR,
        (24, 25, 39, 80, 102, 103, 104, 112, 129, 130, 137, 138, 139, 147),
    ),
    **_codes(
        C.POSITIONING,
        (2, 3, 4, 5, 6, 7, 50, 51, 52, 53, 54, 57, 93, 94, 109, 111, 113, 128, 132, 144, 145),
    ),
    **_codes(C.DOCKING, (16, 17, 30, 37, 48, 90, 116, 118, 133, 153, 161)),
    **_codes(
        C.COMMUNICATION,
        (75, 76, 77, 701, 702, 703, 704, 705, 706, 707, 708, 709, 710, 711, 712, 715, 716, 717),
    ),
    **_codes(C.FIRMWARE, (134, 160, 724)),
}
"""Husqvarna codes by neutral category; every other code is ``other``. Wheel slip (78, "the
situation was not solved with the moving pattern") counts as trapped: the mower needs a hand."""

LIFT_TILT_CODES: Final = frozenset(c for c, cat in CATEGORIES.items() if cat is C.LIFT_TILT)

_INFO_CODES: Final = frozenset({27, 56, 75, 76, 155, 156, 157, 158, 159, 160, 165, 702})
"""Notices the mower reports after a change; nothing to do."""
_THEFT_ALARMS: Final = frozenset({69, 70, 71, 72, 73, 74})
"""The theft protection's alarms (code 74 is the geofence)."""
_DEFECTS: Final = frozenset(
    {
        0, 4, 5, 6, 7, 18, 19, 20, 21, 22, 23, 26, 28, 31, 32, 35, 36, 38, 39, 41, 43, 44,
        45, 46, 47, 49, 66, 67, 77, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 96, 97, 98, 99,
        100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 112, 115, 117, 119, 120, 122,
        125, 127, 135, 137, 138, 139, 141, 142, 143, 149, 152,
    }
)  # fmt: skip
"""Faults the mower cannot clear by itself: a repair or the dealer."""


def severity_of(code: int) -> ErrorSeverity:
    """The severity of ``code``: safety stops and theft alarms critical, defects errors,
    notices info, the rest warnings."""
    if code in LIFT_TILT_CODES or code in _THEFT_ALARMS:
        return ErrorSeverity.CRITICAL
    if code in _INFO_CODES:
        return ErrorSeverity.INFO
    if code in _DEFECTS:
        return ErrorSeverity.ERROR
    return ErrorSeverity.WARNING


def category_of(code: int) -> MowerErrorCategory:
    """The neutral category of ``code`` (``other`` when not listed)."""
    return CATEGORIES.get(code, C.OTHER)


ENGLISH_TITLES: Final[Mapping[int, str]] = {
    0: "Unexpected error",
    1: "Outside working area",
    2: "No loop signal",
    3: "Wrong loop signal",
    4: "Loop sensor problem, front",
    5: "Loop sensor problem, rear",
    6: "Loop sensor problem, left",
    7: "Loop sensor problem, right",
    8: "Wrong PIN code",
    9: "Trapped",
    10: "Upside down",
    11: "Low battery",
    12: "Empty battery",
    13: "No drive",
    14: "Mower lifted",
    15: "Lifted",
    16: "Stuck in charging station",
    17: "Charging station blocked",
    18: "Collision sensor problem, rear",
    19: "Collision sensor problem, front",
    20: "Wheel motor blocked, right",
    21: "Wheel motor blocked, left",
    22: "Wheel drive problem, right",
    23: "Wheel drive problem, left",
    24: "Cutting system blocked",
    25: "Cutting system blocked",
    26: "Invalid sub-device combination",
    27: "Settings restored",
    28: "Memory circuit problem",
    29: "Slope too steep",
    30: "Charging system problem",
    31: "STOP button problem",
    32: "Tilt sensor problem",
    33: "Mower tilted",
    34: "Cutting stopped - slope too steep",
    35: "Wheel motor overloaded, right",
    36: "Wheel motor overloaded, left",
    37: "Charging current too high",
    38: "Electronic problem",
    39: "Cutting motor problem",
    40: "Limited cutting height range",
    41: "Unexpected cutting height adj",
    42: "Limited cutting height range",
    43: "Cutting height problem, drive",
    44: "Cutting height problem, curr",
    45: "Cutting height problem, dir",
    46: "Cutting height blocked",
    47: "Cutting height problem",
    48: "No response from charger",
    49: "Ultrasonic problem",
    50: "Guide 1 not found",
    51: "Guide 2 not found",
    52: "Guide 3 not found",
    53: "GPS navigation problem",
    54: "Weak GPS signal",
    55: "Difficult finding home",
    56: "Guide calibration accomplished",
    57: "Guide calibration failed",
    58: "Temporary battery problem",
    59: "Temporary battery problem",
    60: "Temporary battery problem",
    61: "Temporary battery problem",
    62: "Battery restriction due to ambient temperature",
    63: "Temporary battery problem",
    64: "Temporary battery problem",
    65: "Temporary battery problem",
    66: "Battery problem",
    67: "Battery problem",
    68: "Temporary battery problem",
    69: "Alarm! Mower switched off",
    70: "Alarm! Mower stopped",
    71: "Alarm! Mower lifted",
    72: "Alarm! Mower tilted",
    73: "Alarm! Mower in motion",
    74: "Alarm! Outside geofence",
    75: "Connection changed",
    76: "Connection NOT changed",
    77: "Com board not available",
    78: "Slipped - Mower has Slipped.Situation not solved with moving pattern",
    79: "Invalid battery combination - Invalid combination of different battery types.",
    80: "Cutting system imbalance    Warning",
    81: "Safety function faulty",
    82: "Wheel motor blocked, rear right",
    83: "Wheel motor blocked, rear left",
    84: "Wheel drive problem, rear right",
    85: "Wheel drive problem, rear left",
    86: "Wheel motor overloaded, rear right",
    87: "Wheel motor overloaded, rear left",
    88: "Angular sensor problem",
    89: "Invalid system configuration",
    90: "No power in charging station",
    91: "Switch cord problem",
    92: "Work area not valid",
    93: "No accurate position from satellites",
    94: "Reference station communication problem",
    95: "Folding sensor activated",
    96: "Right brush motor overloaded",
    97: "Left brush motor overloaded",
    98: "Ultrasonic Sensor 1 defect",
    99: "Ultrasonic Sensor 2 defect",
    100: "Ultrasonic Sensor 3 defect",
    101: "Ultrasonic Sensor 4 defect",
    102: "Cutting drive motor 1 defect",
    103: "Cutting drive motor 2 defect",
    104: "Cutting drive motor 3 defect",
    105: "Lift Sensor defect",
    106: "Collision sensor defect",
    107: "Docking sensor defect",
    108: "Folding cutting deck sensor defect",
    109: "Loop sensor defect",
    110: "Collision sensor error",
    111: "No confirmed position",
    112: "Cutting system major imbalance",
    113: "Complex working area",
    114: "Too high discharge current",
    115: "Too high internal current",
    116: "High charging power loss",
    117: "High internal power loss",
    118: "Charging system problem",
    119: "Zone generator problem",
    120: "Internal voltage error",
    121: "High internal temperature",
    122: "CAN error",
    123: "Destination not reachable",
    124: "Destination blocked",
    125: "Battery needs replacement",
    126: "Battery near end of life",
    127: "Battery problem",
    128: "Multiple reference stations detected",
    129: "Auxiliary cutting means blocked",
    130: "Imbalanced auxiliary cutting disc detected",
    131: "Lifted in link arm",
    132: "EPOS accessory missing",
    133: "Bluetooth communication with charging station failed",
    134: "Invalid software configuration",
    135: "Radar problem",
    136: "Work area tampered",
    137: "High temperature in cutting motor, right",
    138: "High temperature in cutting motor, center",
    139: "High temperature in cutting motor, left",
    141: "Wheel brush motor problem",
    142: "Self diagnostic error",
    143: "Accessory power problem",
    144: "Boundary wire problem",
    145: "No correction data available",
    147: "Cutting disc lost",
    148: "Chassis collision",
    149: "Vision system malfunction",
    151: "Poor vision performance",
    152: "Vision system error",
    153: "Charging aborted",
    154: "Water hose found in working area",
    155: "PIN code changed",
    156: "WiFi settings changed",
    157: "Bluetooth settings changed",
    158: "Cellular settings changed",
    159: "Mower installation position changed",
    160: "Firmware has been updated",
    161: "No charging station signal",
    162: "Camera lens wet",
    163: "Camera lens blocked",
    165: "Sitemap changed",
    166: "Partly uncut area",
    701: "Connectivity problem",
    702: "Connectivity settings restored",
    703: "Connectivity problem",
    704: "Connectivity problem",
    705: "Connectivity problem",
    706: "Poor signal quality",
    707: "SIM card requires PIN",
    708: "SIM card locked",
    709: "SIM card not found",
    710: "SIM card locked",
    711: "SIM card locked",
    712: "SIM card locked",
    713: "Geofence problem",
    714: "Geofence problem",
    715: "Connectivity problem",
    716: "Connectivity problem",
    717: "SMS could not be sent",
    724: "Communication circuit board SW must be updated",
}
"""The English names of the codes as aioautomower 3.1.0 (MIT) carries them in
``aioautomower.model.model_mower.ERRORCODES``, copied so that building the lexicon does not
import the vendor library; a test keeps the copy equal to the installed table."""


GERMAN_TITLES: Final[Mapping[int, str]] = {
    0: "Unerwarteter Fehler",
    1: "Außerhalb des Arbeitsbereichs",
    2: "Kein Schleifensignal",
    3: "Falsches Schleifensignal",
    4: "Problem Schleifensensor vorne",
    5: "Problem Schleifensensor hinten",
    6: "Problem Schleifensensor links",
    7: "Problem Schleifensensor rechts",
    8: "Falscher PIN-Code",
    9: "Eingeklemmt",
    10: "Liegt auf dem Kopf",
    11: "Akku schwach",
    12: "Akku leer",
    13: "Kein Antrieb",
    14: "Mäher angehoben",
    15: "Angehoben",
    16: "In der Ladestation festgefahren",
    17: "Ladestation blockiert",
    18: "Problem Stoßsensor hinten",
    19: "Problem Stoßsensor vorne",
    20: "Radmotor rechts blockiert",
    21: "Radmotor links blockiert",
    22: "Problem Radantrieb rechts",
    23: "Problem Radantrieb links",
    24: "Schneidsystem blockiert",
    25: "Schneidsystem blockiert",
    26: "Ungültige Kombination von Baugruppen",
    27: "Einstellungen wiederhergestellt",
    28: "Problem Speicherschaltung",
    29: "Hang zu steil",
    30: "Problem Ladesystem",
    31: "Problem STOP-Taste",
    32: "Problem Neigungssensor",
    33: "Mäher gekippt",
    34: "Mähen gestoppt, Hang zu steil",
    35: "Radmotor rechts überlastet",
    36: "Radmotor links überlastet",
    37: "Ladestrom zu hoch",
    38: "Elektronikproblem",
    39: "Problem Mähmotor",
    40: "Eingeschränkter Schnitthöhenbereich",
    41: "Unerwartete Schnitthöhenverstellung",
    42: "Eingeschränkter Schnitthöhenbereich",
    43: "Problem Schnitthöhe, Antrieb",
    44: "Problem Schnitthöhe, Strom",
    45: "Problem Schnitthöhe, Richtung",
    46: "Schnitthöhe blockiert",
    47: "Problem Schnitthöhe",
    48: "Keine Antwort von der Ladestation",
    49: "Problem Ultraschall",
    50: "Suchkabel 1 nicht gefunden",
    51: "Suchkabel 2 nicht gefunden",
    52: "Suchkabel 3 nicht gefunden",
    53: "Problem GPS-Navigation",
    54: "Schwaches GPS-Signal",
    55: "Ladestation schwer zu finden",
    56: "Suchkabel-Kalibrierung abgeschlossen",
    57: "Suchkabel-Kalibrierung fehlgeschlagen",
    58: "Vorübergehendes Akkuproblem",
    59: "Vorübergehendes Akkuproblem",
    60: "Vorübergehendes Akkuproblem",
    61: "Vorübergehendes Akkuproblem",
    62: "Akku durch Umgebungstemperatur eingeschränkt",
    63: "Vorübergehendes Akkuproblem",
    64: "Vorübergehendes Akkuproblem",
    65: "Vorübergehendes Akkuproblem",
    66: "Akkuproblem",
    67: "Akkuproblem",
    68: "Vorübergehendes Akkuproblem",
    69: "Alarm! Mäher ausgeschaltet",
    70: "Alarm! Mäher gestoppt",
    71: "Alarm! Mäher angehoben",
    72: "Alarm! Mäher gekippt",
    73: "Alarm! Mäher in Bewegung",
    74: "Alarm! Außerhalb des Geofence",
    75: "Verbindung geändert",
    76: "Verbindung nicht geändert",
    77: "Kommunikationsplatine nicht verfügbar",
    78: "Rutscht, Ausweichmanöver ohne Erfolg",
    79: "Ungültige Akkukombination",
    80: "Unwucht im Schneidsystem",
    81: "Sicherheitsfunktion fehlerhaft",
    82: "Radmotor hinten rechts blockiert",
    83: "Radmotor hinten links blockiert",
    84: "Problem Radantrieb hinten rechts",
    85: "Problem Radantrieb hinten links",
    86: "Radmotor hinten rechts überlastet",
    87: "Radmotor hinten links überlastet",
    88: "Problem Winkelsensor",
    89: "Ungültige Systemkonfiguration",
    90: "Kein Strom an der Ladestation",
    91: "Problem Schaltkabel",
    92: "Arbeitsbereich ungültig",
    93: "Keine genaue Position von den Satelliten",
    94: "Problem Kommunikation mit der Referenzstation",
    95: "Klappsensor ausgelöst",
    96: "Bürstenmotor rechts überlastet",
    97: "Bürstenmotor links überlastet",
    98: "Ultraschallsensor 1 defekt",
    99: "Ultraschallsensor 2 defekt",
    100: "Ultraschallsensor 3 defekt",
    101: "Ultraschallsensor 4 defekt",
    102: "Mähantrieb 1 defekt",
    103: "Mähantrieb 2 defekt",
    104: "Mähantrieb 3 defekt",
    105: "Hebesensor defekt",
    106: "Stoßsensor defekt",
    107: "Andocksensor defekt",
    108: "Sensor des Klappmähdecks defekt",
    109: "Schleifensensor defekt",
    110: "Fehler Stoßsensor",
    111: "Keine bestätigte Position",
    112: "Starke Unwucht im Schneidsystem",
    113: "Komplexer Arbeitsbereich",
    114: "Entladestrom zu hoch",
    115: "Interner Strom zu hoch",
    116: "Hohe Verlustleistung beim Laden",
    117: "Hohe interne Verlustleistung",
    118: "Problem Ladesystem",
    119: "Problem Zonengenerator",
    120: "Interner Spannungsfehler",
    121: "Hohe Innentemperatur",
    122: "CAN-Fehler",
    123: "Ziel nicht erreichbar",
    124: "Ziel blockiert",
    125: "Akku muss ersetzt werden",
    126: "Akku fast am Ende seiner Lebensdauer",
    127: "Akkuproblem",
    128: "Mehrere Referenzstationen erkannt",
    129: "Zusätzliches Schneidmittel blockiert",
    130: "Unwucht an zusätzlicher Schneidscheibe",
    131: "Am Lenkerarm angehoben",
    132: "EPOS-Zubehör fehlt",
    133: "Bluetooth-Verbindung zur Ladestation fehlgeschlagen",
    134: "Ungültige Softwarekonfiguration",
    135: "Problem Radar",
    136: "Arbeitsbereich manipuliert",
    137: "Hohe Temperatur im Mähmotor rechts",
    138: "Hohe Temperatur im Mähmotor Mitte",
    139: "Hohe Temperatur im Mähmotor links",
    141: "Problem Radbürstenmotor",
    142: "Fehler der Selbstdiagnose",
    143: "Problem Zubehörstromversorgung",
    144: "Problem Begrenzungskabel",
    145: "Keine Korrekturdaten verfügbar",
    147: "Schneidscheibe verloren",
    148: "Kollision mit dem Gehäuse",
    149: "Störung des Kamerasystems",
    151: "Schlechte Kameraleistung",
    152: "Fehler des Kamerasystems",
    153: "Laden abgebrochen",
    154: "Wasserschlauch im Arbeitsbereich gefunden",
    155: "PIN-Code geändert",
    156: "WLAN-Einstellungen geändert",
    157: "Bluetooth-Einstellungen geändert",
    158: "Mobilfunkeinstellungen geändert",
    159: "Einbauposition des Mähers geändert",
    160: "Firmware wurde aktualisiert",
    161: "Kein Signal der Ladestation",
    162: "Kameralinse nass",
    163: "Kameralinse verdeckt",
    165: "Gartenkarte geändert",
    166: "Teilweise ungemähte Fläche",
    701: "Verbindungsproblem",
    702: "Verbindungseinstellungen wiederhergestellt",
    703: "Verbindungsproblem",
    704: "Verbindungsproblem",
    705: "Verbindungsproblem",
    706: "Schlechte Signalqualität",
    707: "SIM-Karte braucht PIN",
    708: "SIM-Karte gesperrt",
    709: "SIM-Karte nicht gefunden",
    710: "SIM-Karte gesperrt",
    711: "SIM-Karte gesperrt",
    712: "SIM-Karte gesperrt",
    713: "Problem Geofence",
    714: "Problem Geofence",
    715: "Verbindungsproblem",
    716: "Verbindungsproblem",
    717: "SMS konnte nicht gesendet werden",
    724: "Software der Kommunikationsplatine muss aktualisiert werden",
}
"""German titles of every code of aioautomower's table."""


def _t(title: str, description: str, cause: str | None, *steps: str) -> ErrorText:
    return ErrorText(title=title, description=description, cause=cause, steps=list(steps))


@dataclass(frozen=True)
class _Advice:
    """Description and steps for every code of one category, English and German."""

    en: tuple[str, tuple[str, ...]]
    de: tuple[str, tuple[str, ...]]


_ADVICE: Final[Mapping[MowerErrorCategory, _Advice]] = {
    C.WHEEL_SLIP: _Advice(
        en=(
            "The wheels slipped and the mower could not get free by itself.",
            (
                "Check the ground: wet grass, mud or a slope that is too steep.",
                "Clean the wheels and start the mower again.",
            ),
        ),
        de=(
            "Die Räder sind durchgerutscht und der Mäher kam nicht von selbst frei.",
            (
                "Prüfe den Boden: nasses Gras, Matsch oder ein zu steiler Hang.",
                "Reinige die Räder und starte den Mäher erneut.",
            ),
        ),
    ),
    C.TRAPPED: _Advice(
        en=(
            "The mower cannot move on by itself and stopped.",
            ("Free the mower and check what held it.", "Start it again from the mower or MowDeck."),
        ),
        de=(
            "Der Mäher kommt nicht mehr von selbst weiter und hat angehalten.",
            (
                "Befreie den Mäher und prüfe, was ihn festhielt.",
                "Starte ihn am Mäher oder in MowDeck neu.",
            ),
        ),
    ),
    C.OUTSIDE: _Advice(
        en=(
            "The mower is outside the area it may work in.",
            ("Carry the mower back into its working area.", "Check the boundary or the map."),
        ),
        de=(
            "Der Mäher ist außerhalb des Bereichs, in dem er arbeiten darf.",
            (
                "Trag den Mäher zurück in seinen Arbeitsbereich.",
                "Prüfe die Begrenzung oder die Karte.",
            ),
        ),
    ),
    C.LIFT_TILT: _Advice(
        en=(
            "A safety sensor stopped the blades and the wheels.",
            ("Check on the mower now.", "Put it down on level lawn and confirm on the mower."),
        ),
        de=(
            "Ein Sicherheitssensor hat Messer und Räder gestoppt.",
            ("Sieh sofort nach dem Mäher.", "Stell ihn auf ebenen Rasen und bestätige am Mäher."),
        ),
    ),
    C.COLLISION: _Advice(
        en=(
            "The collision sensing reports a fault.",
            (
                "Check the body for damage and dirt.",
                "Restart the mower; if it stays, contact your dealer.",
            ),
        ),
        de=(
            "Die Stoßerkennung meldet einen Fehler.",
            (
                "Prüfe die Karosserie auf Schäden und Schmutz.",
                "Starte den Mäher neu; bleibt der Fehler, wende dich an deinen Händler.",
            ),
        ),
    ),
    C.OBSTACLE: _Advice(
        en=(
            "Something blocks the way the mower needs.",
            ("Clear the way.", "Start the mower again."),
        ),
        de=(
            "Etwas versperrt den Weg, den der Mäher braucht.",
            ("Räum den Weg frei.", "Starte den Mäher erneut."),
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
    C.BLADE_MOTOR: _Advice(
        en=(
            "The cutting system reports a fault.",
            (
                "Switch the mower off and check the blades and the disc for grass and damage.",
                "Restart the mower; if it stays, contact your dealer.",
            ),
        ),
        de=(
            "Das Schneidsystem meldet einen Fehler.",
            (
                "Schalte den Mäher aus und prüfe Messer und Scheibe auf Gras und Schäden.",
                "Starte den Mäher neu; bleibt der Fehler, wende dich an deinen Händler.",
            ),
        ),
    ),
    C.POSITIONING: _Advice(
        en=(
            "The mower cannot tell where it is (boundary wire, guide wire or satellites).",
            (
                "Check the boundary and guide wires or the sky view of the antenna.",
                "Start the mower again.",
            ),
        ),
        de=(
            "Der Mäher weiß nicht, wo er ist (Begrenzungskabel, Suchkabel oder Satelliten).",
            (
                "Prüfe Begrenzungs- und Suchkabel oder die freie Sicht der Antenne zum Himmel.",
                "Starte den Mäher erneut.",
            ),
        ),
    ),
    C.DOCKING: _Advice(
        en=(
            "The charging station or the charging reports a problem.",
            (
                "Check that the charging station has power and nothing blocks it.",
                "Put the mower into the station.",
            ),
        ),
        de=(
            "Die Ladestation oder das Laden meldet ein Problem.",
            (
                "Prüfe, ob die Ladestation Strom hat und nichts sie blockiert.",
                "Stell den Mäher in die Station.",
            ),
        ),
    ),
    C.COMMUNICATION: _Advice(
        en=(
            "The mower's connection to the Husqvarna cloud has a problem.",
            (
                "Check the mobile or Wi-Fi coverage at the mower.",
                "Check the settings in the Automower Connect app.",
            ),
        ),
        de=(
            "Die Verbindung des Mähers zur Husqvarna-Cloud hat ein Problem.",
            (
                "Prüfe den Mobilfunk- oder WLAN-Empfang am Mäher.",
                "Prüfe die Einstellungen in der Automower-Connect-App.",
            ),
        ),
    ),
    C.FIRMWARE: _Advice(
        en=(
            "The mower's software needs attention.",
            ("Update the mower in the Automower Connect app.",),
        ),
        de=(
            "Die Software des Mähers braucht Aufmerksamkeit.",
            ("Aktualisiere den Mäher in der Automower-Connect-App.",),
        ),
    ),
    C.OTHER: _Advice(
        en=(
            "The mower reports a problem.",
            ("Check the mower and its display.", "Restart it; if it stays, contact your dealer."),
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

_INFO_ADVICE: Final = _Advice(
    en=("The mower reports a change; nothing to do.", ()),
    de=("Der Mäher meldet eine Änderung; nichts zu tun.", ()),
)


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
    9: (
        _t(
            "Trapped",
            "The mower cannot get out of where it is and stopped.",
            "A narrow passage, a hollow, a root or something under the mower holds it.",
            "Free the mower and remove what trapped it.",
            "Start it again from the mower or MowDeck.",
            "If it happens at the same place, adjust the boundary there.",
        ),
        _t(
            "Eingeklemmt",
            "Der Mäher kommt von seiner Stelle nicht mehr weg und hat angehalten.",
            "Eine enge Stelle, eine Mulde, eine Wurzel oder etwas unter dem Mäher hält ihn fest.",
            "Befreie den Mäher und entferne, was ihn festhält.",
            "Starte ihn am Mäher oder in MowDeck neu.",
            "Passiert es immer an derselben Stelle, passe dort die Begrenzung an.",
        ),
    ),
    1: (
        _t(
            "Outside the working area",
            "The mower is outside its boundary and cannot start.",
            "It was carried there, or the boundary signal is wrong at that place.",
            "Carry the mower back inside the working area.",
            "Start it again.",
        ),
        _t(
            "Außerhalb des Arbeitsbereichs",
            "Der Mäher steht außerhalb seiner Begrenzung und kann nicht starten.",
            "Er wurde dorthin getragen, oder das Begrenzungssignal stimmt dort nicht.",
            "Trag den Mäher zurück in den Arbeitsbereich.",
            "Starte ihn erneut.",
        ),
    ),
    10: (
        _t(
            "Upside down",
            "The mower lies on its back; the blades and the wheels stopped.",
            "It tipped over on a slope or an edge, or someone turned it over.",
            "Check on the mower now.",
            "Turn it over onto level lawn, look for damage and confirm on the mower.",
        ),
        _t(
            "Liegt auf dem Kopf",
            "Der Mäher liegt auf dem Rücken; Messer und Räder sind gestoppt.",
            "Er ist an einem Hang oder einer Kante gekippt, oder jemand hat ihn umgedreht.",
            "Sieh sofort nach dem Mäher.",
            "Dreh ihn auf ebenen Rasen um, prüfe ihn auf Schäden und bestätige am Mäher.",
        ),
    ),
    14: (_t("Lifted", *_LIFTED_EN), _t("Angehoben", *_LIFTED_DE)),
    15: (_t("Lifted", *_LIFTED_EN), _t("Angehoben", *_LIFTED_DE)),
    131: (
        _t("Lifted at the link arm", *_LIFTED_EN),
        _t("Am Lenkerarm angehoben", *_LIFTED_DE),
    ),
    33: (
        _t(
            "Tilted",
            "The tilt sensor stopped the mower.",
            "The slope is too steep, or someone tipped the mower.",
            "Check on the mower and put it on level lawn.",
            "Confirm on the mower; avoid the steep spot with the boundary.",
        ),
        _t(
            "Gekippt",
            "Der Neigungssensor hat den Mäher gestoppt.",
            "Der Hang ist zu steil, oder jemand hat den Mäher gekippt.",
            "Sieh nach dem Mäher und stell ihn auf ebenen Rasen.",
            "Bestätige am Mäher; nimm die steile Stelle mit der Begrenzung aus.",
        ),
    ),
    71: (
        _t(
            "Alarm: mower lifted",
            "The theft protection raised its alarm: the mower was lifted.",
            "Someone lifted the mower, possibly to take it.",
            "Check on the mower now: MowDeck raised the theft alarm.",
            "Enter the PIN on the mower to stop the alarm.",
        ),
        _t(
            "Alarm: Mäher angehoben",
            "Der Diebstahlschutz hat Alarm ausgelöst: Der Mäher wurde angehoben.",
            "Jemand hat den Mäher hochgehoben, möglicherweise um ihn mitzunehmen.",
            "Sieh sofort nach dem Mäher: MowDeck hat den Diebstahlalarm ausgelöst.",
            "Gib am Mäher die PIN ein, um den Alarm zu beenden.",
        ),
    ),
    72: (
        _t(
            "Alarm: mower tilted",
            "The theft protection raised its alarm: the mower was tilted.",
            "Someone tipped the mower, possibly to take it.",
            "Check on the mower now.",
            "Enter the PIN on the mower to stop the alarm.",
        ),
        _t(
            "Alarm: Mäher gekippt",
            "Der Diebstahlschutz hat Alarm ausgelöst: Der Mäher wurde gekippt.",
            "Jemand hat den Mäher gekippt, möglicherweise um ihn mitzunehmen.",
            "Sieh sofort nach dem Mäher.",
            "Gib am Mäher die PIN ein, um den Alarm zu beenden.",
        ),
    ),
    73: (
        _t(
            "Alarm: mower moved",
            "The theft protection raised its alarm: the mower is moving without a job.",
            "Someone carries or pushes the mower.",
            "Check on the mower now and follow its position in MowDeck.",
        ),
        _t(
            "Alarm: Mäher bewegt",
            "Der Diebstahlschutz hat Alarm ausgelöst: Der Mäher bewegt sich ohne Auftrag.",
            "Jemand trägt oder schiebt den Mäher.",
            "Sieh sofort nach dem Mäher und verfolge seine Position in MowDeck.",
        ),
    ),
    74: (
        _t(
            "Alarm: outside the geofence",
            "The mower left the geofence set in the Automower Connect app.",
            "Someone carried it away, or the geofence is set too tight.",
            "Check on the mower now and follow its position in MowDeck.",
            "If it is where it belongs, check the geofence in the Automower Connect app.",
        ),
        _t(
            "Alarm: außerhalb des Geofence",
            "Der Mäher hat den in der Automower-Connect-App festgelegten Geofence verlassen.",
            "Jemand hat ihn weggetragen, oder der Geofence ist zu eng eingestellt.",
            "Sieh sofort nach dem Mäher und verfolge seine Position in MowDeck.",
            "Steht er, wo er hingehört, prüfe den Geofence in der Automower-Connect-App.",
        ),
    ),
    12: (
        _t(
            "Battery empty",
            "The battery is empty and the mower stopped.",
            "It could not reach the charging station in time.",
            "Carry the mower into its charging station.",
            "If it happens often, check that it finds the station (guide wire, signal).",
        ),
        _t(
            "Akku leer",
            "Der Akku ist leer und der Mäher hat angehalten.",
            "Er hat die Ladestation nicht rechtzeitig erreicht.",
            "Trag den Mäher in seine Ladestation.",
            "Passiert es oft, prüfe, ob er die Station findet (Suchkabel, Signal).",
        ),
    ),
    90: (
        _t(
            "No power at the charging station",
            "The charging station has no power, so the mower cannot charge.",
            "The power supply is unplugged, a fuse tripped or the cable is damaged.",
            "Check the power supply and the cable of the charging station.",
        ),
        _t(
            "Kein Strom an der Ladestation",
            "Die Ladestation hat keinen Strom, der Mäher kann nicht laden.",
            "Das Netzteil steckt nicht, eine Sicherung hat ausgelöst oder das Kabel ist "
            "beschädigt.",
            "Prüfe Netzteil und Kabel der Ladestation.",
        ),
    ),
    93: (
        _t(
            "No accurate satellite position",
            "The mower has no precise position and waits.",
            "Trees, buildings or the weather block the satellites, or the reference station "
            "does not reach it.",
            "Wait a little; the mower carries on once the position is precise again.",
            "If it stays, check the reference station and the sky view around the mower.",
        ),
        _t(
            "Keine genaue Satellitenposition",
            "Der Mäher hat keine genaue Position und wartet.",
            "Bäume, Gebäude oder das Wetter verdecken die Satelliten, oder die Referenzstation "
            "erreicht ihn nicht.",
            "Warte etwas; der Mäher macht weiter, sobald die Position wieder genau ist.",
            "Bleibt es so, prüfe die Referenzstation und die freie Sicht zum Himmel.",
        ),
    ),
}
"""Hand-written words for the codes MowDeck acts on, English and German."""


def _entry(code: int, english: str) -> ErrorLexiconEntry:
    category = category_of(code)
    severity = severity_of(code)
    curated = CURATED.get(code)
    if curated is not None:
        texts = {"en": curated[0], "de": curated[1]}
    else:
        advice = _INFO_ADVICE if severity is ErrorSeverity.INFO else _ADVICE[category]
        texts = {
            "en": ErrorText(
                title=english.strip(), description=advice.en[0], steps=list(advice.en[1])
            ),
            "de": ErrorText(
                title=GERMAN_TITLES.get(code, english.strip()),
                description=advice.de[0],
                steps=list(advice.de[1]),
            ),
        }
        if code not in GERMAN_TITLES:
            texts.pop("de")
    return ErrorLexiconEntry(
        code=str(code),
        category=category,
        severity=severity,
        user_action=severity is not ErrorSeverity.INFO,
        texts=texts,
    )


def build_lexicon() -> ErrorLexicon:
    """The lexicon from aioautomower's table (code 0 is "no error" in the API, so it is left
    out; aioautomower maps it to no error key as well)."""
    entries = [_entry(code, text) for code, text in sorted(ENGLISH_TITLES.items()) if code != 0]
    return ErrorLexicon(
        vendor=VENDOR,
        vendor_name="Husqvarna",
        connection_kinds=[],
        source=SOURCE,
        entries=entries,
    )


def normalise(raw: str) -> str:
    """A raw code in the lexicon's spelling (``"014"`` and ``" 14 "`` give ``"14"``)."""
    text = raw.strip()
    return str(int(text)) if text.isdigit() else text


@cache
def husqvarna_lexicon() -> ErrorLexiconProvider:
    """The entry point object (``mowdeck.error_lexicons``: ``husqvarna``), built once."""
    return ErrorLexiconProvider(build_lexicon(), normalise=normalise)
