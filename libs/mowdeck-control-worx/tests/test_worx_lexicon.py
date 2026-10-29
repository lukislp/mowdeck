"""The Worx error lexicon (ADR 0067): every code of pyworxcloud's table, consistent with
``MowerState.error_category`` and registered through ``mowdeck.error_lexicons``."""

import tomllib
from pathlib import Path
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    ConnectionKind,
    ErrorLexicon,
    ErrorSeverity,
    MowerActivity,
    MowerErrorCategory,
)
from mowdeck_control import ErrorLexiconRegistry, explain_error
from mowdeck_control_worx import lexicon, mapping
from pyworxcloud.utils.state import ERROR_TO_DESCRIPTION, STATE_TO_DESCRIPTION
from worx_support import WIRE, mower

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built() -> ErrorLexicon:
    return lexicon.worx_lexicon().lexicon


def test_the_copies_are_pyworxclouds_tables() -> None:
    assert dict(lexicon.ENGLISH_NAMES) == ERROR_TO_DESCRIPTION  # update with pyworxcloud
    assert dict(lexicon.STATUS_NAMES) == STATE_TO_DESCRIPTION


def test_the_lexicon_covers_every_error_code(built: ErrorLexicon) -> None:
    codes = {e.code for e in built.entries}
    assert codes == {str(c) for c in ERROR_TO_DESCRIPTION if c not in (-1, 0)}
    info = built.info()
    assert info.vendor == "worx" and info.vendor_name == "Worx"
    assert info.locales == ["de", "en"]
    assert info.unverified == len(built.entries)  # reverse engineered, nothing confirmed
    assert built.connection_kinds == []  # found by vendor, never by the shared `cloud` kind


def test_every_entry_has_english_and_german(built: ErrorLexicon) -> None:
    for entry in built.entries:
        assert set(entry.texts) == {"en", "de"}, entry.code
        assert entry.texts["en"].title and entry.texts["de"].title
        assert not entry.verified
        if entry.severity is not ErrorSeverity.INFO:
            assert entry.user_action and entry.texts["de"].steps, entry.code
    assert set(lexicon.GERMAN_TITLES) == {c for c in ERROR_TO_DESCRIPTION if c not in (-1, 0)}


def test_categories_match_the_state_mapping(built: ErrorLexicon) -> None:
    for entry in built.entries:
        code = int(entry.code)
        assert entry.category is lexicon.category_of(code)
        state = mapping.state_of(mower(WIRE, le=code), mower_id=uuid4(), account_id=uuid4())
        if code == 5:  # the rain delay is not an error state
            assert state.activity is not MowerActivity.ERROR and state.error_category is None
            continue
        assert state.error_category is entry.category, code
        assert state.error_code == entry.code


def test_lift_codes_are_critical_and_the_rain_delay_is_info(built: ErrorLexicon) -> None:
    index = built.index()
    assert frozenset({2, 11}) == lexicon.LIFT_TILT_CODES == mapping.LIFT_CODES
    for code in lexicon.LIFT_TILT_CODES:
        entry = index[str(code)]
        assert entry.category is MowerErrorCategory.LIFT_TILT
        assert entry.severity is ErrorSeverity.CRITICAL and entry.user_action
    rain = index["5"]
    assert rain.category is MowerErrorCategory.RAIN
    assert rain.severity is ErrorSeverity.INFO and not rain.user_action
    assert index["3"].severity is ErrorSeverity.ERROR  # wire missing: the installation
    assert index["104"].category is MowerErrorCategory.OTHER  # a slope never raises the alarm


def test_curated_codes_have_description_cause_and_steps(built: ErrorLexicon) -> None:
    index = built.index()
    for code in lexicon.CURATED:
        for text in index[str(code)].texts.values():
            assert text.description and text.cause
            assert text.steps or code == 5


def test_lookups_in_german_and_unknown_codes() -> None:
    registry = ErrorLexiconRegistry(lambda: [])
    registry.register(lexicon.worx_lexicon())
    [lifted] = registry.explain(["002"], "de", vendor="worx")
    assert (lifted.known, lifted.code, lifted.title) == (True, "2", "Angehoben")
    assert lifted.category is MowerErrorCategory.LIFT_TILT and not lifted.verified
    [english] = registry.explain(["1"], "en", vendor="worx")
    assert english.title == "Trapped" and len(english.steps) == 3
    [plain] = registry.explain(["116"], "de", vendor="worx")
    assert plain.title == "Schnitthöhenverstellung blockiert"
    assert plain.category is MowerErrorCategory.BLADE_MOTOR
    [unknown] = registry.explain(["987"], "de", vendor="worx")
    assert not unknown.known
    assert registry.provider(connection=ConnectionKind.CLOUD) is None
    assert lexicon.normalise(" 011 ") == "11" and lexicon.normalise("x") == "x"


def test_the_lexicon_is_installed_as_an_entry_point() -> None:
    project = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["entry-points"]["mowdeck.error_lexicons"]["worx"] == (
        "mowdeck_control_worx.lexicon:worx_lexicon"
    )
    assert explain_error("11", "en", vendor="worx").title == "Upside down"
