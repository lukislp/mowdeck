"""The caller's language for texts that come from adapters (ADR 0066): the ``locale`` query
parameter, else the first language of ``Accept-Language``, else English (the same rule as the
error lexicon routes of Telemetry)."""

from typing import Annotated

from fastapi import Header, Query

LocaleQuery = Annotated[
    str | None,
    Query(
        pattern=r"^[A-Za-z]{2}([-_][A-Za-z0-9]{1,8})?$",
        description="Language of the texts (`de`, `en`; `de-DE` works too). Default: the first "
        "language of `Accept-Language`, else `en`; a language without texts falls back to `en`.",
    ),
]
AcceptLanguage = Annotated[str | None, Header(include_in_schema=False)]


def request_locale(locale: str | None, accept_language: str | None) -> str | None:
    """``locale``, else the first language of ``Accept-Language``."""
    if locale:
        return locale
    if accept_language:
        return accept_language.split(",", 1)[0].split(";", 1)[0].strip() or None
    return None
