"""The error lexicon of the simulated mower (vendor ``simulated``, ADR 0065).

It explains the two codes :class:`~mowdeck_control.simulator.SimulatedMower` reports by itself
(``lifted`` and ``stuck``) and is registered like every vendor lexicon, through the entry point
group ``mowdeck.error_lexicons``. A code injected with ``inject_error`` stays unknown, which
shows the generic fallback.
"""

from mowdeck_contracts.v1 import (
    ConnectionKind,
    ErrorLexicon,
    ErrorLexiconEntry,
    ErrorSeverity,
    ErrorText,
    MowerErrorCategory,
)

from mowdeck_control.simulator import LIFT_ERROR_CODE, STUCK_ERROR_CODE

SIMULATED_LEXICON = ErrorLexicon(
    vendor="simulated",
    vendor_name="Simulator",
    connection_kinds=[ConnectionKind.SIMULATED],
    source="mowdeck-control (written for the simulator)",
    entries=[
        ErrorLexiconEntry(
            code=STUCK_ERROR_CODE,
            category=MowerErrorCategory.TRAPPED,
            severity=ErrorSeverity.WARNING,
            texts={
                "en": ErrorText(
                    title="Stuck on an obstacle",
                    description="The simulated mower ran into an obstacle and stopped its job.",
                    cause="The simulator's stuck timer of the job ran out.",
                    steps=[
                        "Drive the mower free with the joystick or let AI assist propose a way.",
                        "Resume the job once it stands on open lawn.",
                    ],
                ),
                "de": ErrorText(
                    title="Steckt an einem Hindernis fest",
                    description="Der simulierte Mäher ist auf ein Hindernis gefahren und hat "
                    "seinen Auftrag angehalten.",
                    cause="Der Hängenbleiben-Zeitgeber des Simulators ist abgelaufen.",
                    steps=[
                        "Fahr den Mäher mit dem Joystick frei oder lass die KI-Hilfe einen Weg "
                        "vorschlagen.",
                        "Setze den Auftrag fort, sobald er auf freiem Rasen steht.",
                    ],
                ),
            },
        ),
        ErrorLexiconEntry(
            code=LIFT_ERROR_CODE,
            category=MowerErrorCategory.LIFT_TILT,
            severity=ErrorSeverity.CRITICAL,
            texts={
                "en": ErrorText(
                    title="Lifted",
                    description="The lift sensor of the simulated mower stopped the blades.",
                    cause="Someone lifted the mower (here: the simulator's lift command).",
                    steps=[
                        "Check whether anyone from your household lifted the mower.",
                        "Put it back on flat ground and confirm on the mower to clear the error.",
                    ],
                ),
                "de": ErrorText(
                    title="Angehoben",
                    description="Der Hebesensor des simulierten Mähers hat die Messer gestoppt.",
                    cause="Jemand hat den Mäher hochgehoben (hier: der Anheben-Befehl des "
                    "Simulators).",
                    steps=[
                        "Prüfe, ob jemand aus deinem Haushalt den Mäher angehoben hat.",
                        "Stell ihn zurück auf ebenen Boden und bestätige am Mäher, um den "
                        "Fehler zu löschen.",
                    ],
                ),
            },
        ),
    ],
)
"""The simulator's lexicon (entry point ``mowdeck.error_lexicons``, name ``simulated``)."""
