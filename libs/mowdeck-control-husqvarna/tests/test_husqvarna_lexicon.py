"""The Husqvarna error lexicon (ADR 0067): every code of aioautomower's table, consistent with
``MowerState.error_category`` and registered through ``mowdeck.error_lexicons``."""

import tomllib
from pathlib import Path
from uuid import uuid4

import pytest
from aioautomower.model.model_mower import ERRORCODES
from husqvarna_support import LISA_LIFTED, attributes
from mowdeck_contracts.v1 import (
    ConnectionKind,
    ErrorLexicon,
    ErrorSeverity,
    MowerErrorCategory,
)
from mowdeck_control import ErrorLexiconRegistry, explain_error
from mowdeck_control_husqvarna import lexicon, mapping

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built() -> ErrorLexicon:
    return lexicon.husqvarna_lexicon().lexicon


def test_the_lexicon_covers_the_whole_table(built: ErrorLexicon) -> None:
    codes = {e.code for e in built.entries}
    assert codes == {str(c) for c in ERRORCODES if c != 0}
    info = built.info()
    assert info.vendor == "husqvarna" and info.vendor_name == "Husqvarna"
    assert info.locales == ["de", "en"] and info.unverified == 0
    assert built.connection_kinds == []  # found by vendor, never by the shared `cloud` kind


def test_every_entry_has_english_and_german(built: ErrorLexicon) -> None:
    for entry in built.entries:
        assert set(entry.texts) == {"en", "de"}, entry.code
        assert entry.texts["en"].title and entry.texts["de"].title
        if entry.severity is not ErrorSeverity.INFO:
            assert entry.user_action and entry.texts["de"].steps, entry.code
    assert set(lexicon.GERMAN_TITLES) == set(ERRORCODES)


def test_the_english_titles_are_aioautomowers_table() -> None:
    assert dict(lexicon.ENGLISH_TITLES) == ERRORCODES  # update the copy with aioautomower


def test_categories_match_the_state_mapping(built: ErrorLexicon) -> None:
    for entry in built.entries:
        code = int(entry.code)
        assert entry.category is lexicon.category_of(code)
        state = mapping.state_of(
            attributes(LISA_LIFTED, errorCode=code), mower_id=uuid4(), account_id=uuid4()
        )
        assert state.error_category is entry.category, code


def test_lift_and_tilt_codes_are_critical_safety_stops(built: ErrorLexicon) -> None:
    index = built.index()
    assert frozenset({10, 14, 15, 33, 71, 72, 131}) == lexicon.LIFT_TILT_CODES
    assert mapping.LIFT_CODES <= lexicon.LIFT_TILT_CODES
    for code in lexicon.LIFT_TILT_CODES:
        entry = index[str(code)]
        assert entry.category is MowerErrorCategory.LIFT_TILT
        assert entry.severity is ErrorSeverity.CRITICAL and entry.user_action
    assert index["73"].severity is ErrorSeverity.CRITICAL  # theft alarm: in motion
    assert index["160"].severity is ErrorSeverity.INFO and not index["160"].user_action
    assert index["105"].severity is ErrorSeverity.ERROR  # a defect


def test_curated_codes_have_description_cause_and_steps(built: ErrorLexicon) -> None:
    index = built.index()
    for code in lexicon.CURATED:
        for text in index[str(code)].texts.values():
            assert text.description and text.cause and text.steps


def test_lookups_in_german_and_unknown_codes() -> None:
    registry = ErrorLexiconRegistry(lambda: [])
    registry.register(lexicon.husqvarna_lexicon())
    [lifted] = registry.explain(["015"], "de", vendor="husqvarna")
    assert (lifted.known, lifted.code, lifted.title) == (True, "15", "Angehoben")
    assert lifted.category is MowerErrorCategory.LIFT_TILT
    [english] = registry.explain(["9"], "en", vendor="husqvarna")
    assert english.title == "Trapped" and len(english.steps) == 3
    [plain] = registry.explain(["706"], "de", vendor="husqvarna")
    assert plain.title == "Schlechte Signalqualität"
    assert plain.category is MowerErrorCategory.COMMUNICATION
    [unknown] = registry.explain(["987"], "de", vendor="husqvarna")
    assert not unknown.known
    # not picked for the shared cloud kind, so other vendors' cloud mowers keep theirs
    assert registry.provider(connection=ConnectionKind.CLOUD) is None
    assert lexicon.normalise(" 014 ") == "14" and lexicon.normalise("x") == "x"


def test_the_lexicon_is_installed_as_an_entry_point() -> None:
    project = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["entry-points"]["mowdeck.error_lexicons"]["husqvarna"] == (
        "mowdeck_control_husqvarna.lexicon:husqvarna_lexicon"
    )
    assert explain_error("71", "en", vendor="husqvarna").title == "Alarm: mower lifted"
