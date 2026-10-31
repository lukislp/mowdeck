"""Display text people type in: no control or format characters (label spoofing)."""

import pytest
from mowdeck_contracts.v1 import PlainText, clean_area_name, clean_text
from pydantic import BaseModel, Field, ValidationError

RLO = "\u202e"
ZWSP = "\u200b"
ZWJ = "\u200d"
ZWNJ = "\u200c"
FAMILY = f"\U0001f468{ZWJ}\U0001f469{ZWJ}\U0001f467"
PERSIAN = f"\u0645\u06cc{ZWNJ}\u062e\u0648\u0627\u0647\u0645"
HEBREW_ARABIC = "\u05e2\u05d1\u05e8\u05d9\u05ea \u0627\u0644\u0639\u0631\u0628\u064a\u0629"


class Named(BaseModel):
    name: PlainText = Field(min_length=1, max_length=10)
    note: PlainText | None = None


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Front lawn", "Front lawn"),
        (f"{RLO}nwal", "nwal"),
        (f"La{ZWSP}wn", "Lawn"),
        ("\u202aa\u202cb\u2066c\u2069d\u200ee\u200ff", "abcdef"),
        ("\ufeffBOM", "BOM"),
        ("soft\u00adhyphen", "softhyphen"),
        ("bell\x07\x00", "bell"),
        ("two\nlines\tand\r\ntabs", "two lines and  tabs"),
        ("para\u2029graph\u2028line\x85next", "para graph line next"),
        ("Rasen \U0001f331 S\u00fcd", "Rasen \U0001f331 S\u00fcd"),
        (FAMILY, FAMILY),
        (PERSIAN, PERSIAN),
        (HEBREW_ARABIC, HEBREW_ARABIC),
        (f"{ZWJ}{ZWNJ}", ""),
        (f" {ZWJ}\u3000{ZWNJ}\n", ""),
        ("   ", ""),
        (f"a{ZWJ}", f"a{ZWJ}"),
    ],
)
def test_clean_text(raw: str, clean: str) -> None:
    assert clean_text(raw) == clean


def test_plain_text_cleans_before_the_length_checks() -> None:
    assert Named(name=f"{RLO}Lawn{ZWSP}").name == "Lawn"
    assert Named(name="a" * 10 + ZWSP * 5).name == "a" * 10
    assert Named(name="x", note=f"a{RLO}b").note == "ab"
    assert Named(name="x", note=None).note is None
    with pytest.raises(ValidationError):
        Named(name=RLO + ZWSP)
    with pytest.raises(ValidationError):
        Named(name=ZWJ)
    with pytest.raises(ValidationError):
        Named(name=f" {ZWNJ} {ZWJ} ")
    assert Named(name="x", note=f"{ZWJ} ").note == ""
    with pytest.raises(ValidationError):
        Named(name=123)  # type: ignore[arg-type]


def test_area_names_lose_invisible_characters() -> None:
    assert clean_area_name(f" Side{ZWSP} {RLO}strip ") == "Side strip"
    assert clean_area_name(RLO) is None
    assert clean_area_name(f"{ZWJ}{ZWNJ}") is None
    assert clean_area_name(f" {ZWJ} ") is None
