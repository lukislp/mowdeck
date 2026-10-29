"""Error lexicon contracts (ADR 0067): entries, lookup with locale fallback, unknown codes."""

import pytest
from mowdeck_contracts.v1 import (
    ConnectionKind,
    ErrorExplainRequest,
    ErrorLexicon,
    ErrorLexiconEntry,
    ErrorLexiconSearchRequest,
    ErrorSeverity,
    ErrorText,
    MowerErrorCategory,
    explain_code,
    generic_text,
    lexicon_for_connection,
    normalise_locale,
    search_lexicon,
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


def _search(lexicon: ErrorLexicon, **values: object) -> list[str]:
    reply = search_lexicon(lexicon, ErrorLexiconSearchRequest.model_validate(values))
    return [e.code for e in reply.entries]


def test_contracts_error_lexicon_search_filters_orders_and_pages() -> None:
    lexicon = _lexicon().model_copy(
        update={
            "entries": [
                *_lexicon().entries,
                ErrorLexiconEntry(code="10", texts={"en": ErrorText(title="Ten blocked")}),
                ErrorLexiconEntry(code="E2", texts={"en": ErrorText(title="Letters")}),
            ]
        }
    )
    assert _search(lexicon) == ["7", "9", "10", "E2"]  # numbers numerically, then the rest
    assert _search(lexicon, query="LIFTED") == ["7"]
    assert _search(lexicon, query="angehoben", locale="de") == ["7"]  # the locale's words
    assert _search(lexicon, query="someone", locale="de") == ["7"]  # and English
    assert _search(lexicon, query="ten blocked") == ["10"]  # every word must match
    assert _search(lexicon, query="ten lifted") == []
    assert _search(lexicon, query="-7") == ["7"]  # aliases
    assert _search(lexicon, category="lift_tilt") == ["7"]
    assert _search(lexicon, severity="warning") == ["9", "10", "E2"]
    assert _search(lexicon, codes=["-7", "10", "nope"]) == ["7", "10"]
    reply = search_lexicon(lexicon, ErrorLexiconSearchRequest(offset=1, limit=2, locale="de"))
    assert reply.total == 4 and reply.offset == 1
    assert [e.code for e in reply.entries] == ["9", "10"]
    assert reply.entries[0].verified is False and reply.entries[0].locale == "en"
    assert reply.lexicon is not None and reply.lexicon.vendor == "acme"
    assert reply.categories == {MowerErrorCategory.LIFT_TILT: 1, MowerErrorCategory.OTHER: 3}
    assert reply.severities == {ErrorSeverity.CRITICAL: 1, ErrorSeverity.WARNING: 3}
    with pytest.raises(ValidationError):
        ErrorLexiconSearchRequest(limit=101)
