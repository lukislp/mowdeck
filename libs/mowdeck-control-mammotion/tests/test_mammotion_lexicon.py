"""The Mammotion error lexicon (ADR 0065): built from PyMammotion's table plus curated texts,
registered through ``mowdeck.error_lexicons`` and consistent with ``MowerState.error_category``."""

import tomllib
from pathlib import Path

import pytest
from mowdeck_contracts.v1 import ConnectionKind, ErrorLexicon, ErrorSeverity, MowerErrorCategory
from mowdeck_control import ErrorLexiconRegistry, explain_error
from mowdeck_control_mammotion import errors, lexicon
from mowdeck_control_mammotion.port import bundled_error_codes

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built() -> ErrorLexicon:
    return lexicon.mammotion_lexicon().lexicon


def test_mammotion_lexicon_covers_the_whole_table(built: ErrorLexicon) -> None:
    table = {k for k, row in bundled_error_codes().items() if k not in lexicon.SKIPPED_CODES}
    codes = {e.code for e in built.entries}
    assert table <= codes
    assert set(map(str, lexicon.CURATED)) <= codes
    info = built.info()
    assert info.entries == len(codes) > 600
    assert 0 < info.unverified < 40
    assert info.locales == ["de", "en"]
    assert built.connection_kinds == [ConnectionKind.CLOUD, ConnectionKind.BLE_BRIDGE]


def test_mammotion_lexicon_categories_match_the_state_mapping(
    built: ErrorLexicon,
) -> None:
    for entry in built.entries:
        assert entry.category is MowerErrorCategory(errors.category_of(entry.code)), entry.code
        assert entry.aliases == [f"-{entry.code}"]


def test_mammotion_lexicon_curated_codes_have_both_languages(
    built: ErrorLexicon,
) -> None:
    index = built.index()
    for code in lexicon.CURATED:
        entry = index[str(code)]
        assert set(entry.texts) == {"en", "de"}
        for text in entry.texts.values():
            assert text.description and text.steps
    for code in errors.LIFT_TILT_CODES:
        entry = index[str(code)]
        assert entry.category is MowerErrorCategory.LIFT_TILT
        assert entry.severity is ErrorSeverity.CRITICAL
    for code in errors.RECOVERABLE_CODES:
        assert code in lexicon.CURATED


def test_mammotion_lexicon_marks_firmware_only_codes_unverified(
    built: ErrorLexicon,
) -> None:
    index = built.index()
    assert index["11131"].verified is False  # read out of the firmware by PyMammotion
    assert index["5004"].verified is False
    assert index["1201"].verified is True
    assert index["1400"].verified is True


def test_mammotion_lexicon_explains_negative_codes_in_german() -> None:
    registry = ErrorLexiconRegistry(lambda: [])
    registry.register(lexicon.mammotion_lexicon())
    [stuck] = registry.explain(["-1201"], "de", connection=ConnectionKind.CLOUD)
    assert (stuck.known, stuck.code, stuck.title) == (True, "1201", "Steckt fest")
    assert stuck.category is MowerErrorCategory.TRAPPED and len(stuck.steps) == 3
    [raw] = registry.explain(["1400"], "de", vendor="mammotion")
    assert raw.title == "Kein Speicher mehr" and raw.locale == "de"
    [english_only] = registry.explain(["11112"], "de", vendor="mammotion")
    assert english_only.locale == "de"  # curated
    [unknown] = registry.explain(["987654"], "de", vendor="mammotion")
    assert unknown.title == "Unbekannter Fehler 987654 von Mammotion"


def test_mammotion_lexicon_is_installed_as_an_entry_point() -> None:
    project = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["entry-points"]["mowdeck.error_lexicons"]["mammotion"] == (
        "mowdeck_control_mammotion.lexicon:mammotion_lexicon"
    )
    assert explain_error("-2801", "en", vendor="mammotion").title == "Lifted"


@pytest.mark.parametrize(
    ("solution", "steps"),
    [
        (
            "Restart the robot. If the issue persists, contact the after-sales support",
            ["Restart the robot.", "If the issue persists, contact the after-sales support"],
        ),
        (
            "Needs help.1. Check the wheels.2. Restart the robot.If it persists, call us.",
            ["Needs help.", "Check the wheels.", "Restart the robot.", "If it persists, call us."],
        ),
        ("Please handle promptly.\u200b\u200b", ["Please handle promptly."]),
        ("", []),
    ],
)
def test_mammotion_lexicon_splits_vendor_solutions(solution: str, steps: list[str]) -> None:
    assert lexicon.split_steps(solution) == steps


def test_mammotion_lexicon_cleans_placeholders() -> None:
    assert lexicon.clean("Out of task areas (%s)") == "Out of task areas"
    assert lexicon.normalise("-1201") == "1201" and lexicon.normalise(" x ") == "x"
