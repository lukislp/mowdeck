"""The error lexicon registry (ADR 0065): entry points, normalisation, fallback, simulator."""

import logging
import tomllib
from collections.abc import Iterable
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from mowdeck_contracts.v1 import (
    ConnectionKind,
    ErrorLexicon,
    ErrorLexiconEntry,
    ErrorText,
    MowerErrorCategory,
)
from mowdeck_control import (
    STUCK_ERROR_CODE,
    ErrorLexiconProvider,
    ErrorLexiconRegistry,
    default_error_lexicons,
    explain_error,
)
from mowdeck_control.simulator import LIFT_ERROR_CODE
from mowdeck_control.simulator_lexicon import SIMULATED_LEXICON

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

ACME = ErrorLexicon(
    vendor="acme",
    vendor_name="Acme",
    connection_kinds=[ConnectionKind.CLOUD],
    entries=[
        ErrorLexiconEntry(
            code="12",
            category=MowerErrorCategory.BATTERY,
            texts={"en": ErrorText(title="Battery low"), "de": ErrorText(title="Akku leer")},
        )
    ],
)


def _acme_factory() -> ErrorLexiconProvider:
    return ErrorLexiconProvider(ACME, normalise=lambda code: code.lstrip("-E"))


def _broken() -> ErrorLexicon:
    raise RuntimeError("table missing")


JUNK = 42

_OBJECTS = {"ACME": ACME, "_acme_factory": _acme_factory, "_broken": _broken, "JUNK": JUNK}


def _registry(*points: tuple[str, object]) -> ErrorLexiconRegistry:
    """A registry whose entry points name objects of this module."""
    names = {id(value): name for name, value in _OBJECTS.items()}

    def source() -> Iterable[EntryPoint]:
        return [
            EntryPoint(name=name, value=f"{__name__}:{names[id(value)]}", group="g")
            for name, value in points
        ]

    return ErrorLexiconRegistry(source)


def test_control_error_lexicons_load_factories_and_normalise() -> None:
    registry = _registry(("acme", _acme_factory))
    assert registry.vendors() == ["acme"]
    provider = registry.provider("acme")
    assert provider is not None and provider.code_of("-12") == "12"
    [explanation] = registry.explain(["E12"], "de", vendor="acme")
    assert (explanation.known, explanation.code, explanation.title) == (True, "12", "Akku leer")
    assert provider.category_of("-12") is MowerErrorCategory.BATTERY
    assert provider.category_of("99") is None


def test_control_error_lexicons_find_the_lexicon_by_connection() -> None:
    registry = _registry(("acme", ACME))
    [explanation] = registry.explain(["12"], "en", connection=ConnectionKind.CLOUD)
    assert explanation.vendor == "acme" and explanation.title == "Battery low"
    [unknown] = registry.explain(["12"], "en", connection=ConnectionKind.SIMULATED)
    assert unknown.known is False and unknown.vendor is None
    assert registry.provider() is None


def test_control_error_lexicons_skip_broken_and_misnamed_entry_points(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, "mowdeck_control.error_lexicons")
    registry = _registry(
        ("broken", _broken), ("other", ACME), ("junk", JUNK), ("acme", ACME), ("acme", ACME)
    )
    assert registry.vendors() == ["acme"]
    assert "cannot load error lexicon" in caplog.text
    assert "it is for vendor 'acme'" in caplog.text
    assert "keeping the registered error lexicon" in caplog.text
    assert [info.vendor for info in registry.infos()] == ["acme"]


def test_control_error_lexicons_register_refuses_a_second_lexicon() -> None:
    registry = _registry()
    registry.register(ACME)
    registry.register(ACME)  # the same lexicon again: a no-op
    other = ACME.model_copy(update={"vendor_name": "Acme Inc."})
    with pytest.raises(ValueError, match="already registered"):
        registry.register(other)
    assert registry.register(other, replace=True).lexicon.vendor_name == "Acme Inc."
    with pytest.raises(TypeError):
        registry.register(lambda: 42)  # type: ignore[arg-type,return-value]


def test_control_error_lexicons_failing_normaliser_keeps_the_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def bad(code: str) -> str:
        raise ValueError(code)

    provider = ErrorLexiconProvider(ACME, normalise=bad)
    assert provider.code_of(" x ") == "x"
    assert "normalising code" in caplog.text
    assert provider.explain("x").known is False


def test_control_error_lexicons_simulator_lexicon_is_installed() -> None:
    assert "simulated" in default_error_lexicons().vendors()
    stuck = explain_error(STUCK_ERROR_CODE, "de", connection=ConnectionKind.SIMULATED)
    assert (stuck.known, stuck.vendor, stuck.title) == (
        True,
        "simulated",
        "Steckt an einem Hindernis fest",
    )
    lifted = explain_error(LIFT_ERROR_CODE, "en", vendor="simulated")
    assert lifted.category is MowerErrorCategory.LIFT_TILT and lifted.steps
    injected = explain_error("E42", "de", vendor="simulated")
    assert injected.known is False and injected.title == "Unbekannter Fehler E42 von Simulator"


def test_control_error_lexicons_simulator_entries_match_its_categories() -> None:
    from mowdeck_control.simulator import _ERROR_CATEGORY

    for entry in SIMULATED_LEXICON.entries:
        assert _ERROR_CATEGORY[entry.code] is entry.category
        assert set(entry.texts) == {"en", "de"}


def test_control_error_lexicons_entry_point_is_declared() -> None:
    project = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["entry-points"]["mowdeck.error_lexicons"]["simulated"] == (
        "mowdeck_control.simulator_lexicon:SIMULATED_LEXICON"
    )
