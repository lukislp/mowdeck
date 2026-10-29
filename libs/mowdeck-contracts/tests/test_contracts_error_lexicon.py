"""Error lexicon contracts (ADR 0065): entries, lookup with locale fallback, unknown codes."""

import pytest
from mowdeck_contracts.v1 import (
    ConnectionKind,
    ErrorExplainRequest,
    ErrorLexicon,
    ErrorLexiconEntry,
    ErrorSeverity,
    ErrorText,
    MowerErrorCategory,
    explain_code,
    generic_text,
    lexicon_for_connection,
    normalise_locale,
    unknown_explanation,
)
from pydantic import ValidationError


def _lexicon() -> ErrorLexicon:
    return ErrorLexicon(
        vendor="acme",
        vendor_name="Acme",
        connection_kinds=[ConnectionKind.CLOUD],
        entries=[
            ErrorLexiconEntry(
                code="7",
                aliases=["-7"],
                category=MowerErrorCategory.LIFT_TILT,
                severity=ErrorSeverity.CRITICAL,
                texts={
                    "en": ErrorText(title="Lifted", cause="Someone lifted it", steps=["Look"]),
                    "de": ErrorText(title="Angehoben", steps=["Nachsehen"]),
                },
            ),
            ErrorLexiconEntry(
                code="9", verified=False, texts={"en": ErrorText(title="Only English")}
            ),
        ],
    )


def test_contracts_error_lexicon_explains_in_the_locale() -> None:
    lexicon = _lexicon()
    de = explain_code(lexicon, "7", "de-DE")
    assert (de.known, de.locale, de.title, de.steps) == (True, "de", "Angehoben", ["Nachsehen"])
    assert (de.vendor, de.vendor_name, de.category) == (
        "acme",
        "Acme",
        MowerErrorCategory.LIFT_TILT,
    )
    assert de.severity is ErrorSeverity.CRITICAL and de.user_action
    en = explain_code(lexicon, "-7", "en")
    assert (en.code, en.title, en.cause) == ("7", "Lifted", "Someone lifted it")


def test_contracts_error_lexicon_falls_back_to_english_and_marks_unverified() -> None:
    explanation = explain_code(_lexicon(), "9", "de")
    assert (explanation.locale, explanation.title) == ("en", "Only English")
    assert explanation.verified is False


def test_contracts_error_lexicon_unknown_code_names_the_vendor() -> None:
    explanation = explain_code(_lexicon(), "1234", "en")
    assert explanation.known is False
    assert explanation.title == "Unknown error 1234 from Acme"
    assert explanation.category is MowerErrorCategory.OTHER and explanation.steps
    german = explain_code(None, "1234", "de", category=MowerErrorCategory.LIFT_TILT)
    assert german.title == "Unbekannter Fehler 1234 von dem Mäher"
    assert german.description == generic_text(MowerErrorCategory.LIFT_TILT, "de").title
    assert german.severity is ErrorSeverity.CRITICAL and german.vendor is None


def test_contracts_error_lexicon_unknown_locale_is_english() -> None:
    assert normalise_locale("fr_FR") == "fr"
    assert unknown_explanation("1", "fr").locale == "en"
    assert normalise_locale(None) == normalise_locale("") == normalise_locale("x1") == "en"


@pytest.mark.parametrize("category", list(MowerErrorCategory))
def test_contracts_error_lexicon_generic_text_for_every_category(
    category: MowerErrorCategory,
) -> None:
    for locale in ("en", "de"):
        text = generic_text(category, locale)
        assert text.title and text.steps


def test_contracts_error_lexicon_rejects_duplicates_and_missing_english() -> None:
    with pytest.raises(ValidationError, match="twice"):
        ErrorLexicon(
            vendor="acme",
            vendor_name="Acme",
            entries=[
                ErrorLexiconEntry(code="1", texts={"en": ErrorText(title="a")}),
                ErrorLexiconEntry(code="2", aliases=["1"], texts={"en": ErrorText(title="b")}),
            ],
        )
    with pytest.raises(ValidationError, match="'en' texts"):
        ErrorLexiconEntry(code="1", texts={"de": ErrorText(title="nur deutsch")})
    with pytest.raises(ValidationError):
        ErrorLexicon(vendor="Acme Corp", vendor_name="Acme")


def test_contracts_error_lexicon_lookup_by_connection_and_info() -> None:
    lexicon = _lexicon()
    assert lexicon_for_connection([lexicon], ConnectionKind.CLOUD) is lexicon
    assert lexicon_for_connection([lexicon], "cloud") is lexicon
    assert lexicon_for_connection([lexicon], ConnectionKind.SIMULATED) is None
    assert lexicon_for_connection([lexicon], None) is None
    info = lexicon.info()
    assert (info.entries, info.unverified, info.locales) == (2, 1, ["de", "en"])


def test_contracts_error_lexicon_request_limits() -> None:
    assert ErrorExplainRequest(codes=["1"]).locale == "en"
    with pytest.raises(ValidationError):
        ErrorExplainRequest(codes=[])
    with pytest.raises(ValidationError):
        ErrorExplainRequest(codes=[str(n) for n in range(21)])
