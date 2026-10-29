"""Error lexicons: vendor error codes in plain words (ADR 0067).

A mower reports a vendor code (``MowerState.error_code``, e.g. Mammotion's ``1201``). An *error
lexicon* explains the codes of one vendor: title, description, likely cause, the steps that fix
it, a severity, the manufacturer-neutral category and whether the user has to act. Every adapter
package ships the lexicon of its vendor and registers it through the entry point group
``mowdeck.error_lexicons`` (``mowdeck-control``); nothing here names a manufacturer.

Texts are kept per locale (``en`` is required, ``de`` is the second language of MowDeck); a
missing locale falls back to English. :func:`explain_code` turns a code into an
:class:`ErrorExplanation` and never fails: a code the lexicon does not know, or no lexicon at
all, gives an explanation with ``known`` false and generic advice for the category, when one is
known.

The Device-Gateway, where the adapters are installed, answers lookups on the core NATS subjects
``gateway.errors.explain`` (:class:`ErrorExplainRequest` / :class:`ErrorExplainReply`),
``gateway.errors.lexicons`` (:class:`ErrorLexiconListRequest` / :class:`ErrorLexiconListReply`)
and ``gateway.errors.entries`` (:class:`ErrorLexiconSearchRequest` /
:class:`ErrorLexiconSearchReply`, a searchable page of one lexicon), queue group
``device-gateway``.
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import Field, StringConstraints, model_validator

from mowdeck_contracts.v1._base import PROVIDER_ID_PATTERN, ContractModel
from mowdeck_contracts.v1.mower import ConnectionKind
from mowdeck_contracts.v1.mower_status import MowerErrorCategory
from mowdeck_contracts.v1.subjects import (
    GATEWAY_ERRORS_ENTRIES,
    GATEWAY_ERRORS_EXPLAIN,
    GATEWAY_ERRORS_LEXICONS,
)

DEFAULT_LOCALE: Final = "en"
"""The locale every lexicon entry has and every lookup falls back to."""
LEXICON_LOCALES: Final = ("en", "de")
"""The locales MowDeck shows; a lexicon may carry more."""
MAX_EXPLAIN_CODES: Final = 20
"""Most codes one :class:`ErrorExplainRequest` may ask for."""
MAX_SEARCH_PAGE: Final = 100
"""Most entries one :class:`ErrorLexiconSearchReply` carries."""
MAX_SEARCH_CODES: Final = 200
"""Most codes one :class:`ErrorLexiconSearchRequest` may filter by."""

VendorKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
"""Lower-case key of a vendor (``mammotion``, ``simulated``); the entry point name."""
LocaleKey = Annotated[str, StringConstraints(pattern=r"^[a-z]{2}$")]
"""A two-letter language code (``en``, ``de``)."""
ErrorCodeText = Annotated[str, StringConstraints(min_length=1, max_length=64)]
ProviderKey = Annotated[str, StringConstraints(pattern=PROVIDER_ID_PATTERN)]
"""A cloud provider id (ADR 0066), as ``MowerState.cloud_provider`` carries it."""


class ErrorSeverity(StrEnum):
    """How bad an error is for the user, manufacturer neutral."""

    INFO = "info"
    """The mower handles it by itself (waits for the rain to stop, charges first)."""
    WARNING = "warning"
    """The mower stopped and waits; a person should look at it soon."""
    ERROR = "error"
    """A fault the mower cannot clear by itself; it needs a repair or support."""
    CRITICAL = "critical"
    """A safety stop that needs attention right now (lifted, tilted)."""


class ErrorText(ContractModel):
    """The words of one lexicon entry in one locale."""

    title: str = Field(min_length=1, max_length=200, description="Short headline.")
    description: str = Field(
        default="", max_length=2000, description="What happened, in a sentence or two."
    )
    cause: str | None = Field(
        default=None, max_length=1000, description="The likely cause; null when not known."
    )
    steps: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] = Field(
        default_factory=list,
        max_length=12,
        description="What to do, in order; empty when there is nothing to do.",
    )


class ErrorLink(ContractModel):
    """Further reading for an entry (the vendor's help page, a manual chapter)."""

    label: str = Field(min_length=1, max_length=120)
    url: str = Field(pattern=r"^https://", max_length=500)


class ErrorLexiconEntry(ContractModel):
    """One vendor code with its explanation."""

    code: ErrorCodeText = Field(description="The code as `MowerState.error_code` carries it.")
    aliases: list[ErrorCodeText] = Field(
        default_factory=list,
        max_length=16,
        description="Other spellings of the same code (e.g. with a sign or prefix).",
    )
    category: MowerErrorCategory = MowerErrorCategory.OTHER
    severity: ErrorSeverity = ErrorSeverity.WARNING
    user_action: bool = Field(
        default=True, description="A person has to do something before the mower carries on."
    )
    texts: dict[LocaleKey, ErrorText] = Field(
        description="The words per locale; `en` is required, others are optional."
    )
    links: list[ErrorLink] = Field(default_factory=list, max_length=8)
    verified: bool = Field(
        default=True,
        description="False when the meaning of the code could not be checked against a vendor "
        "source (e.g. a code only seen from mowers); clients mark such texts.",
    )

    @model_validator(mode="after")
    def _has_default_locale(self) -> Self:
        if DEFAULT_LOCALE not in self.texts:
            msg = f"an entry needs the {DEFAULT_LOCALE!r} texts"
            raise ValueError(msg)
        return self


class ErrorLexicon(ContractModel):
    """The error codes of one vendor, as an adapter package ships them."""

    vendor: VendorKey
    vendor_name: str = Field(min_length=1, max_length=64, description="Shown to users.")
    connection_kinds: list[ConnectionKind] = Field(
        default_factory=list,
        description="The connection kinds whose mowers report codes of this vendor; a lookup by "
        "`MowerState.connection` finds the lexicon through them.",
    )
    cloud_providers: list[ProviderKey] = Field(
        default_factory=list,
        max_length=16,
        description="The cloud providers (ADR 0066) whose mowers report codes of this vendor; a "
        "lookup by `MowerState.cloud_provider` finds the lexicon through them. Empty: the "
        "provider whose id equals `vendor`.",
    )
    source: str | None = Field(
        default=None, max_length=500, description="Where the entries come from (provenance)."
    )
    entries: list[ErrorLexiconEntry] = Field(default_factory=list)

    def serves_cloud_provider(self, provider: str) -> bool:
        """The mowers of ``provider`` report this vendor's codes (listed, or the vendor key)."""
        return provider in self.cloud_providers or (
            not self.cloud_providers and provider == self.vendor
        )

    @model_validator(mode="after")
    def _unique_codes(self) -> Self:
        seen: set[str] = set()
        for entry in self.entries:
            for code in (entry.code, *entry.aliases):
                if code in seen:
                    msg = f"code {code!r} appears twice in the {self.vendor} lexicon"
                    raise ValueError(msg)
                seen.add(code)
        return self

    def index(self) -> dict[str, ErrorLexiconEntry]:
        """Every code and alias to its entry."""
        result: dict[str, ErrorLexiconEntry] = {}
        for entry in self.entries:
            result[entry.code] = entry
            for alias in entry.aliases:
                result[alias] = entry
        return result

    def info(self) -> "ErrorLexiconInfo":
        """The summary of this lexicon."""
        locales = sorted({locale for e in self.entries for locale in e.texts} | {DEFAULT_LOCALE})
        return ErrorLexiconInfo(
            vendor=self.vendor,
            vendor_name=self.vendor_name,
            connection_kinds=list(self.connection_kinds),
            cloud_providers=list(self.cloud_providers),
            source=self.source,
            entries=len(self.entries),
            unverified=sum(1 for e in self.entries if not e.verified),
            locales=locales,
        )


class ErrorLexiconInfo(ContractModel):
    """Summary of an installed lexicon (no entries)."""

    vendor: VendorKey
    vendor_name: str
    connection_kinds: list[ConnectionKind] = Field(default_factory=list)
    cloud_providers: list[ProviderKey] = Field(
        default_factory=list, description="The cloud providers it serves (empty: `vendor`)."
    )
    source: str | None = None
    entries: int = Field(ge=0, description="Number of codes.")
    unverified: int = Field(ge=0, description="Codes whose meaning could not be verified.")
    locales: list[LocaleKey] = Field(default_factory=list)


class ErrorExplanation(ContractModel):
    """A vendor code in plain words, in one locale."""

    code: ErrorCodeText
    vendor: VendorKey | None = Field(
        default=None, description="The lexicon that explained the code; null when none applied."
    )
    vendor_name: str | None = None
    known: bool = Field(description="The lexicon knows the code.")
    locale: LocaleKey = Field(description="The locale of the texts (after fallback).")
    title: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    cause: str | None = Field(default=None, max_length=1000)
    steps: list[str] = Field(default_factory=list)
    severity: ErrorSeverity
    category: MowerErrorCategory
    user_action: bool
    links: list[ErrorLink] = Field(default_factory=list)
    verified: bool = Field(
        default=True, description="False for an entry whose meaning is not verified."
    )


class ErrorExplainRequest(ContractModel):
    """Ask the Device-Gateway to explain codes (``gateway.errors.explain``).

    The lexicon is picked by ``vendor``; without it by ``cloud_provider`` (the mower's
    ``MowerState.cloud_provider``: the lexicon that serves the provider, none when no lexicon
    does); without both by ``connection`` (the lexicon that lists the kind; for ``cloud`` the
    gateway prefers the provider that claims unqualified cloud registrations). ``category`` is
    the state's ``error_category``: it shapes the generic advice for a code no lexicon knows.
    """

    codes: list[ErrorCodeText] = Field(min_length=1, max_length=MAX_EXPLAIN_CODES)
    vendor: VendorKey | None = None
    cloud_provider: ProviderKey | None = None
    connection: ConnectionKind | None = None
    locale: LocaleKey = DEFAULT_LOCALE
    category: MowerErrorCategory | None = None


class ErrorExplainReply(ContractModel):
    """One explanation per requested code, in request order."""

    explanations: list[ErrorExplanation] = Field(default_factory=list)


class ErrorLexiconListRequest(ContractModel):
    """Ask the Device-Gateway for its installed lexicons (``gateway.errors.lexicons``)."""


class ErrorLexiconListReply(ContractModel):
    """The installed lexicons, sorted by vendor."""

    lexicons: list[ErrorLexiconInfo] = Field(default_factory=list)


class ErrorLexiconSearchRequest(ContractModel):
    """Browse one lexicon (``gateway.errors.entries``): a page of its entries, explained in
    ``locale``, filtered by text, category, severity and codes (all optional, combined with and).

    The lexicon is picked like for :class:`ErrorExplainRequest`: by ``vendor``, else by
    ``cloud_provider``, else by ``connection``.
    """

    vendor: VendorKey | None = None
    cloud_provider: ProviderKey | None = None
    connection: ConnectionKind | None = None
    locale: LocaleKey = DEFAULT_LOCALE
    query: str = Field(
        default="",
        max_length=100,
        description="Matches the code, its aliases and the texts in `locale` and English (case "
        "insensitive, every word must match); empty: everything.",
    )
    category: MowerErrorCategory | None = None
    severity: ErrorSeverity | None = None
    codes: list[ErrorCodeText] = Field(
        default_factory=list,
        max_length=MAX_SEARCH_CODES,
        description="Only these codes (as the lexicon or the mower spells them); empty: all.",
    )
    offset: int = Field(default=0, ge=0, le=100_000)
    limit: int = Field(default=50, ge=1, le=MAX_SEARCH_PAGE)


class ErrorLexiconSearchReply(ContractModel):
    """A page of a lexicon's entries, ordered by code (numbers numerically)."""

    lexicon: ErrorLexiconInfo | None = Field(
        default=None, description="The lexicon searched; null when none applies (or none answered)."
    )
    total: int = Field(default=0, ge=0, description="Entries matching the filters.")
    offset: int = Field(default=0, ge=0)
    entries: list[ErrorExplanation] = Field(default_factory=list)
    categories: dict[MowerErrorCategory, int] = Field(
        default_factory=dict,
        description="Entries per category in the whole lexicon (for filter chips).",
    )
    severities: dict[ErrorSeverity, int] = Field(
        default_factory=dict, description="Entries per severity in the whole lexicon."
    )


# --- Lookup ----------------------------------------------------------------------------------

_GENERIC: Final[Mapping[str, Mapping[MowerErrorCategory, tuple[str, tuple[str, ...]]]]] = {
    "en": {
        MowerErrorCategory.TRAPPED: (
            "The mower is stuck",
            (
                "Look at the mower and remove what holds it (branches, a hollow, a toy).",
                "Lift it out only after the blades have stopped, then resume the job.",
            ),
        ),
        MowerErrorCategory.OBSTACLE: (
            "Something blocks the way",
            ("Clear the path in front of the mower.", "Resume the job or send it home."),
        ),
        MowerErrorCategory.OUTSIDE: (
            "The mower is outside its area",
            (
                "Drive or carry the mower back into a mowing area or onto the dock.",
                "Check that the map still matches the garden.",
            ),
        ),
        MowerErrorCategory.WHEEL_SLIP: (
            "The wheels are slipping",
            (
                "Check for wet grass, mud or a steep slope.",
                "Clean the wheels and drive the mower onto firm ground.",
            ),
        ),
        MowerErrorCategory.LIFT_TILT: (
            "The mower was lifted or tilted",
            (
                "Check on the mower now: was it lifted by someone?",
                "Put it on flat ground and unlock it on the mower to continue.",
            ),
        ),
        MowerErrorCategory.COLLISION: (
            "The bumper was pressed",
            ("Free the bumper and remove the obstacle.", "Resume the job."),
        ),
        MowerErrorCategory.BATTERY: (
            "Battery problem",
            ("Let the mower charge in its dock.", "If it persists, contact the vendor's support."),
        ),
        MowerErrorCategory.BLADE_MOTOR: (
            "Blade or motor fault",
            (
                "Switch the mower off and check the blades and wheels for grass or debris.",
                "Restart the mower; if it persists, contact the vendor's support.",
            ),
        ),
        MowerErrorCategory.RAIN: (
            "Rain detected",
            ("Nothing to do: the mower waits until it is dry.",),
        ),
        MowerErrorCategory.POSITIONING: (
            "Positioning problem",
            (
                "Wait a few minutes in open sky for the position to recover.",
                "Check the RTK base station: power, antenna, clear view of the sky.",
            ),
        ),
        MowerErrorCategory.COMMUNICATION: (
            "Connection problem",
            (
                "Check the mower's Wi-Fi or mobile signal where it stands.",
                "Restart the mower if it stays offline.",
            ),
        ),
        MowerErrorCategory.DOCKING: (
            "Problem with the charging station",
            (
                "Check that nothing blocks the dock and the charging contacts are clean.",
                "Make sure the dock has power and has not been moved.",
            ),
        ),
        MowerErrorCategory.FIRMWARE: (
            "Firmware problem",
            ("Charge the mower and try the update again.", "Restart the mower if it persists."),
        ),
        MowerErrorCategory.OTHER: (
            "The mower reports an error",
            (
                "Look at the mower and its display.",
                "Restart the mower; if it persists, contact the vendor's support.",
            ),
        ),
    },
    "de": {
        MowerErrorCategory.TRAPPED: (
            "Der Mäher steckt fest",
            (
                "Sieh nach dem Mäher und entferne, was ihn festhält (Äste, eine Mulde, Spielzeug).",
                "Heb ihn erst heraus, wenn die Messer stehen, und setze dann den Auftrag fort.",
            ),
        ),
        MowerErrorCategory.OBSTACLE: (
            "Etwas versperrt den Weg",
            (
                "Räum den Weg vor dem Mäher frei.",
                "Setze den Auftrag fort oder schick ihn zur Ladestation.",
            ),
        ),
        MowerErrorCategory.OUTSIDE: (
            "Der Mäher ist außerhalb seines Bereichs",
            (
                "Fahr oder trag den Mäher zurück in einen Mähbereich oder in die Ladestation.",
                "Prüfe, ob die Karte noch zum Garten passt.",
            ),
        ),
        MowerErrorCategory.WHEEL_SLIP: (
            "Die Räder drehen durch",
            (
                "Prüfe auf nasses Gras, Matsch oder einen zu steilen Hang.",
                "Reinige die Räder und fahr den Mäher auf festen Boden.",
            ),
        ),
        MowerErrorCategory.LIFT_TILT: (
            "Der Mäher wurde angehoben oder gekippt",
            (
                "Sieh sofort nach dem Mäher: Hat ihn jemand hochgehoben?",
                "Stell ihn auf ebenen Boden und entsperre ihn am Mäher, um weiterzumachen.",
            ),
        ),
        MowerErrorCategory.COLLISION: (
            "Der Stoßfänger wurde ausgelöst",
            ("Befreie den Stoßfänger und entferne das Hindernis.", "Setze den Auftrag fort."),
        ),
        MowerErrorCategory.BATTERY: (
            "Problem mit dem Akku",
            (
                "Lass den Mäher in der Ladestation laden.",
                "Wenn es bleibt, wende dich an den Support des Herstellers.",
            ),
        ),
        MowerErrorCategory.BLADE_MOTOR: (
            "Fehler an Messer oder Motor",
            (
                "Schalte den Mäher aus und prüfe Messer und Räder auf Gras oder Fremdkörper.",
                "Starte den Mäher neu; wenn es bleibt, wende dich an den Support des Herstellers.",
            ),
        ),
        MowerErrorCategory.RAIN: (
            "Regen erkannt",
            ("Nichts zu tun: Der Mäher wartet, bis es trocken ist.",),
        ),
        MowerErrorCategory.POSITIONING: (
            "Problem mit der Positionierung",
            (
                "Warte ein paar Minuten unter freiem Himmel, bis die Position zurückkommt.",
                "Prüfe die RTK-Basisstation: Strom, Antenne, freie Sicht zum Himmel.",
            ),
        ),
        MowerErrorCategory.COMMUNICATION: (
            "Verbindungsproblem",
            (
                "Prüfe das WLAN- oder Mobilfunksignal des Mähers dort, wo er steht.",
                "Starte den Mäher neu, wenn er offline bleibt.",
            ),
        ),
        MowerErrorCategory.DOCKING: (
            "Problem mit der Ladestation",
            (
                "Prüfe, dass nichts die Ladestation versperrt und die Ladekontakte sauber sind.",
                "Stell sicher, dass die Ladestation Strom hat und nicht verschoben wurde.",
            ),
        ),
        MowerErrorCategory.FIRMWARE: (
            "Problem mit der Firmware",
            (
                "Lade den Mäher und versuche das Update erneut.",
                "Starte den Mäher neu, wenn es bleibt.",
            ),
        ),
        MowerErrorCategory.OTHER: (
            "Der Mäher meldet einen Fehler",
            (
                "Sieh nach dem Mäher und seiner Anzeige.",
                "Starte den Mäher neu; wenn es bleibt, wende dich an den Support des Herstellers.",
            ),
        ),
    },
}
_UNKNOWN: Final[Mapping[str, tuple[str, str]]] = {
    "en": ("Unknown error {code} from {vendor}", "MowDeck has no explanation for this code yet."),
    "de": (
        "Unbekannter Fehler {code} von {vendor}",
        "Für diesen Code hat MowDeck noch keine Erklärung.",
    ),
}
_SOME_VENDOR: Final[Mapping[str, str]] = {"en": "the mower", "de": "dem Mäher"}

GENERIC_SEVERITY: Final[Mapping[MowerErrorCategory, ErrorSeverity]] = {
    MowerErrorCategory.LIFT_TILT: ErrorSeverity.CRITICAL,
    MowerErrorCategory.BLADE_MOTOR: ErrorSeverity.ERROR,
    MowerErrorCategory.RAIN: ErrorSeverity.INFO,
}
"""Severity of a category when no lexicon entry says otherwise (``warning`` if not listed)."""


def normalise_locale(locale: str | None) -> str:
    """``de-DE`` and ``de_AT`` give ``de``; an empty or odd value gives ``en``."""
    base = (locale or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return base if len(base) == 2 and base.isalpha() else DEFAULT_LOCALE


def generic_text(category: MowerErrorCategory, locale: str) -> ErrorText:
    """MowDeck's own advice for a category (no vendor words), in ``locale`` or English."""
    table = _GENERIC.get(locale, _GENERIC[DEFAULT_LOCALE])
    title, steps = table[category]
    return ErrorText(title=title, steps=list(steps))


def unknown_explanation(
    code: str,
    locale: str | None = None,
    *,
    vendor: str | None = None,
    vendor_name: str | None = None,
    category: MowerErrorCategory | None = None,
) -> ErrorExplanation:
    """The explanation of a code no lexicon knows: "Unknown error 1234 from <vendor>" with the
    generic advice of ``category`` (``other`` when not known)."""
    lang = normalise_locale(locale)
    lang = lang if lang in _UNKNOWN else DEFAULT_LOCALE
    kind = category or MowerErrorCategory.OTHER
    title, description = _UNKNOWN[lang]
    who = vendor_name or _SOME_VENDOR[lang]
    generic = generic_text(kind, lang)
    known_kind = category is not None and category is not MowerErrorCategory.OTHER
    return ErrorExplanation(
        code=code,
        vendor=vendor,
        vendor_name=vendor_name,
        known=False,
        locale=lang,
        title=title.format(code=code, vendor=who)[:200],
        description=generic.title if known_kind else description,
        steps=list(generic.steps),
        severity=GENERIC_SEVERITY.get(kind, ErrorSeverity.WARNING),
        category=kind,
        user_action=kind is not MowerErrorCategory.RAIN,
        verified=True,
    )


def explain_code(
    lexicon: ErrorLexicon | None,
    code: str,
    locale: str | None = None,
    *,
    category: MowerErrorCategory | None = None,
    index: Mapping[str, ErrorLexiconEntry] | None = None,
) -> ErrorExplanation:
    """Explain ``code`` with ``lexicon`` in ``locale`` (falling back to English).

    ``index`` is :meth:`ErrorLexicon.index` when the caller keeps it. Without a lexicon or for an
    unknown code: :func:`unknown_explanation` with ``category``.
    """
    entries = index if index is not None else (lexicon.index() if lexicon else {})
    entry = entries.get(code.strip())
    if lexicon is None or entry is None:
        return unknown_explanation(
            code,
            locale,
            vendor=lexicon.vendor if lexicon else None,
            vendor_name=lexicon.vendor_name if lexicon else None,
            category=category,
        )
    lang = normalise_locale(locale)
    if lang not in entry.texts:
        lang = DEFAULT_LOCALE
    text = entry.texts[lang]
    return ErrorExplanation(
        code=entry.code,
        vendor=lexicon.vendor,
        vendor_name=lexicon.vendor_name,
        known=True,
        locale=lang,
        title=text.title,
        description=text.description,
        cause=text.cause,
        steps=list(text.steps),
        severity=entry.severity,
        category=entry.category,
        user_action=entry.user_action,
        links=list(entry.links),
        verified=entry.verified,
    )


def _code_order(code: str) -> tuple[int, int, str]:
    """Numbers first, numerically (``2`` before ``10``), then the rest alphabetically."""
    stripped = code.lstrip("-")
    if stripped.isdigit():
        return (0, int(stripped), code)
    return (1, 0, code.lower())


def _haystack(entry: ErrorLexiconEntry, locale: str) -> str:
    parts = [entry.code, *entry.aliases]
    for lang in {locale, DEFAULT_LOCALE}:
        text = entry.texts.get(lang)
        if text is not None:
            parts.extend([text.title, text.description, text.cause or "", *text.steps])
    return " ".join(parts).casefold()


def search_lexicon(
    lexicon: ErrorLexicon,
    request: ErrorLexiconSearchRequest,
    *,
    index: Mapping[str, ErrorLexiconEntry] | None = None,
) -> ErrorLexiconSearchReply:
    """A page of the entries of ``lexicon`` that match ``request`` (its vendor and connection
    are not checked here). ``request.codes`` are looked up in ``index`` (code and aliases), so a
    caller normalises the mower's spelling first."""
    entries = index if index is not None else lexicon.index()
    lang = normalise_locale(request.locale)
    words = request.query.casefold().split()
    wanted: set[str] | None = None
    if request.codes:
        wanted = {e.code for c in request.codes if (e := entries.get(c.strip())) is not None}
    matching = [
        entry
        for entry in lexicon.entries
        if (wanted is None or entry.code in wanted)
        and (request.category is None or entry.category is request.category)
        and (request.severity is None or entry.severity is request.severity)
        and all(word in _haystack(entry, lang) for word in words)
    ]
    matching.sort(key=lambda entry: _code_order(entry.code))
    page = matching[request.offset : request.offset + request.limit]
    categories: dict[MowerErrorCategory, int] = {}
    severities: dict[ErrorSeverity, int] = {}
    for entry in lexicon.entries:
        categories[entry.category] = categories.get(entry.category, 0) + 1
        severities[entry.severity] = severities.get(entry.severity, 0) + 1
    return ErrorLexiconSearchReply(
        lexicon=lexicon.info(),
        total=len(matching),
        offset=request.offset,
        entries=[explain_code(lexicon, entry.code, lang, index=entries) for entry in page],
        categories=categories,
        severities=severities,
    )


def lexicon_for_connection(
    lexicons: Iterable[ErrorLexicon], connection: ConnectionKind | str | None
) -> ErrorLexicon | None:
    """The first lexicon (by vendor) that lists ``connection``; None when none does."""
    if connection is None:
        return None
    for lexicon in sorted(lexicons, key=lambda lex: lex.vendor):
        if any(kind == connection for kind in lexicon.connection_kinds):
            return lexicon
    return None


__all__ = [
    "DEFAULT_LOCALE",
    "GATEWAY_ERRORS_ENTRIES",
    "GATEWAY_ERRORS_EXPLAIN",
    "GATEWAY_ERRORS_LEXICONS",
    "GENERIC_SEVERITY",
    "LEXICON_LOCALES",
    "MAX_EXPLAIN_CODES",
    "MAX_SEARCH_CODES",
    "MAX_SEARCH_PAGE",
    "ErrorCodeText",
    "ErrorExplainReply",
    "ErrorExplainRequest",
    "ErrorExplanation",
    "ErrorLexicon",
    "ErrorLexiconEntry",
    "ErrorLexiconInfo",
    "ErrorLexiconListReply",
    "ErrorLexiconListRequest",
    "ErrorLexiconSearchReply",
    "ErrorLexiconSearchRequest",
    "ErrorLink",
    "ErrorSeverity",
    "ErrorText",
    "LocaleKey",
    "ProviderKey",
    "VendorKey",
    "explain_code",
    "generic_text",
    "lexicon_for_connection",
    "normalise_locale",
    "search_lexicon",
    "unknown_explanation",
]
