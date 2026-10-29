"""Routes of the error lexicon under ``/api/v1/telemetry`` (ADR 0067).

Vendor error codes in plain words: title, description, likely cause, steps, severity and the
manufacturer-neutral category, in the requested locale (``locale``, else the first language of
``Accept-Language``, else English). The mower routes need ``can_view`` on the mower (404
otherwise) and pick the lexicon by the mower's connection; the vendor routes need any signed-in
principal. A code no lexicon knows, or a gateway that does not answer, still gets an answer
(``known`` false, generic advice).
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Response
from mowdeck_contracts.v1 import (
    MAX_EXPLAIN_CODES,
    MAX_SEARCH_CODES,
    MAX_SEARCH_PAGE,
    ErrorExplanation,
    ErrorLexiconInfo,
    ErrorLexiconSearchReply,
    ErrorSeverity,
    MowerErrorCategory,
)
from mowdeck_service_kit import CurrentAccountId, CurrentPrincipal
from pydantic import BaseModel, Field

from telemetry.adapters.api.deps import ServicesDep, ViewMower
from telemetry.application.errors import ErrorExplanations

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

LocaleQuery = Annotated[
    str | None,
    Query(
        pattern=r"^[A-Za-z]{2}([-_][A-Za-z0-9]{1,8})?$",
        description="Language of the texts (`de`, `en`; `de-DE` works too). Default: the first "
        "language of `Accept-Language`, else `en`; a language without texts falls back to `en`.",
    ),
]
AcceptLanguage = Annotated[str | None, Header(include_in_schema=False)]


class MowerErrorOut(BaseModel):
    """``GET /mowers/{id}/error``: the mower's current error in plain words."""

    mower_id: UUID
    observed_at: datetime = Field(description="When the mower reported the state.")
    activity: str = Field(description="`MowerActivity` of the state.")
    error: ErrorExplanation | None = Field(
        description="The current error explained; null while the mower reports none."
    )


class ErrorExplanationList(BaseModel):
    """Explanations in the order of the requested codes."""

    explanations: list[ErrorExplanation]


class ErrorLexiconList(BaseModel):
    """The installed error lexicons, one per vendor."""

    lexicons: list[ErrorLexiconInfo]


def _locale(locale: str | None, accept_language: str | None) -> str | None:
    if locale:
        return locale
    if accept_language:
        return accept_language.split(",", 1)[0].split(";", 1)[0].strip() or None
    return None


def _errors(services: ServicesDep) -> ErrorExplanations:
    explanations = services.errors
    assert explanations is not None  # noqa: S101 - wired at startup
    return explanations


@router.get("/mowers/{mower_id}/error")
async def get_mower_error(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
    locale: LocaleQuery = None,
    accept_language: AcceptLanguage = None,
) -> MowerErrorOut:
    """The mower's current error (`MowerState.error_code`) in plain words, with the lexicon of
    its vendor; `error` is null while it reports none. 404 "no telemetry yet" before the first
    state."""
    response.headers["Cache-Control"] = "private, max-age=15"
    current = await _errors(services).current(
        account_id, mower_id, _locale(locale, accept_language)
    )
    return MowerErrorOut(
        mower_id=current.mower_id,
        observed_at=current.observed_at,
        activity=current.activity,
        error=current.explanation,
    )


@router.get("/mowers/{mower_id}/error-codes")
async def explain_mower_codes(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    code: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=64)]],
        Query(
            min_length=1,
            max_length=MAX_EXPLAIN_CODES,
            description="Codes as the mower reports them; repeat the parameter for more.",
        ),
    ],
    locale: LocaleQuery = None,
    accept_language: AcceptLanguage = None,
) -> ErrorExplanationList:
    """Explain codes of this mower (e.g. the error counts of its health page) with the lexicon of
    its vendor. 404 "no telemetry yet" before the first state."""
    explanations = await _errors(services).explain_for_mower(
        account_id, mower_id, code, _locale(locale, accept_language)
    )
    return ErrorExplanationList(explanations=explanations)


@router.get("/mowers/{mower_id}/error-lexicon")
async def browse_mower_lexicon(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
    q: Annotated[
        str,
        Query(
            max_length=100,
            description="Search the code and the texts (every word must match); empty: all.",
        ),
    ] = "",
    category: Annotated[MowerErrorCategory | None, Query()] = None,
    severity: Annotated[ErrorSeverity | None, Query()] = None,
    code: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=64)]] | None,
        Query(
            max_length=MAX_SEARCH_CODES,
            description="Only these codes (e.g. the ones this mower reported); repeatable.",
        ),
    ] = None,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_PAGE)] = 50,
    locale: LocaleQuery = None,
    accept_language: AcceptLanguage = None,
) -> ErrorLexiconSearchReply:
    """Browse the error lexicon of this mower's vendor (picked by its connection): a page of
    entries in plain words, ordered by code, filtered by text, category, severity and codes,
    with the counts per category and severity for filter chips. `lexicon` is null when no
    lexicon applies or the Device-Gateway does not answer. 404 "no telemetry yet" before the
    first state."""
    response.headers["Cache-Control"] = "private, max-age=60"
    return await _errors(services).browse_for_mower(
        account_id,
        mower_id,
        _locale(locale, accept_language),
        query=q,
        category=category,
        severity=severity,
        codes=code or [],
        offset=offset,
        limit=limit,
    )


@router.get("/error-codes/{vendor}/{code}")
async def explain_vendor_code(
    vendor: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{0,31}$")],
    code: Annotated[str, Path(min_length=1, max_length=64)],
    _: CurrentPrincipal,
    services: ServicesDep,
    response: Response,
    locale: LocaleQuery = None,
    accept_language: AcceptLanguage = None,
) -> ErrorExplanation:
    """Explain one code of a vendor (the lexicon's key, see `GET /error-lexicons`), e.g.
    `/error-codes/mammotion/1201?locale=de`. An unknown vendor or code answers 200 with `known`
    false."""
    response.headers["Cache-Control"] = "private, max-age=3600"
    [explanation] = await _errors(services).explain(
        [code], _locale(locale, accept_language), vendor=vendor
    )
    return explanation


@router.get("/error-lexicons")
async def list_error_lexicons(_: CurrentPrincipal, services: ServicesDep) -> ErrorLexiconList:
    """The installed error lexicons (vendor key and name, connection kinds, number of codes and
    of unverified ones, locales); empty while the Device-Gateway does not answer."""
    return ErrorLexiconList(lexicons=await _errors(services).lexicons())
