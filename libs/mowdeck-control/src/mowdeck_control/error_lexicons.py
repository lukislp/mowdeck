"""Registry of error lexicons, one per vendor (ADR 0067).

Every adapter package ships the lexicon of its vendor's error codes
(:class:`~mowdeck_contracts.v1.ErrorLexicon`) and registers it through the Python entry point
group ``mowdeck.error_lexicons``: the entry point name is the vendor key, the object is one of

* an :class:`~mowdeck_contracts.v1.ErrorLexicon` (plain data),
* an :class:`ErrorLexiconProvider` (the data plus an optional ``normalise`` function that turns a
  raw code into the lexicon's spelling, e.g. ``-1201`` into ``1201``),
* a callable without arguments that returns either (built lazily, at the first lookup).

Example from ``mowdeck-control-mammotion``::

    [project.entry-points."mowdeck.error_lexicons"]
    mammotion = "mowdeck_control_mammotion.lexicon:mammotion_lexicon"

This package registers the simulator's lexicon (``simulated``) the same way. Lookups never
fail: an unknown vendor, an unknown code or a lexicon that cannot be loaded gives the generic
explanation of :func:`~mowdeck_contracts.v1.unknown_explanation`.

Which lexicon explains a mower's code (:meth:`ErrorLexiconRegistry.provider`): the one of an
explicit ``vendor``; else the one that serves the mower's cloud provider
(``MowerState.cloud_provider``, ADR 0066: several vendors share the connection kind ``cloud``,
so the kind alone cannot tell them apart); else the first (by vendor) that lists the mower's
connection kind; else none, and the generic explanation.
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points

from mowdeck_contracts.v1 import (
    ConnectionKind,
    ErrorExplanation,
    ErrorLexicon,
    ErrorLexiconEntry,
    ErrorLexiconInfo,
    ErrorLexiconSearchReply,
    ErrorLexiconSearchRequest,
    MowerErrorCategory,
    explain_code,
    search_lexicon,
    unknown_explanation,
)

ENTRY_POINT_GROUP = "mowdeck.error_lexicons"

CodeNormaliser = Callable[[str], str]
EntryPointSource = Callable[[], Iterable[EntryPoint]]

_log = logging.getLogger("mowdeck_control.error_lexicons")


@dataclass(frozen=True)
class ErrorLexiconProvider:
    """A lexicon with the optional function that maps a raw vendor code to its spelling."""

    lexicon: ErrorLexicon
    normalise: CodeNormaliser | None = None
    _index: dict[str, ErrorLexiconEntry] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        self._index.update(self.lexicon.index())

    @property
    def vendor(self) -> str:
        """The vendor key."""
        return self.lexicon.vendor

    def code_of(self, raw: str) -> str:
        """``raw`` in the lexicon's spelling (unchanged without ``normalise`` or when it fails)."""
        code = raw.strip()
        if self.normalise is None or code in self._index:
            return code
        try:
            return self.normalise(code)
        except Exception:
            _log.warning("normalising code %r of %s failed", code, self.vendor, exc_info=True)
            return code

    def entry(self, raw: str) -> ErrorLexiconEntry | None:
        """The entry of ``raw``, None when the lexicon does not know it."""
        return self._index.get(self.code_of(raw))

    def explain(
        self, raw: str, locale: str | None = None, *, category: MowerErrorCategory | None = None
    ) -> ErrorExplanation:
        """Explain ``raw`` in ``locale`` (see :func:`~mowdeck_contracts.v1.explain_code`)."""
        return explain_code(
            self.lexicon, self.code_of(raw), locale, category=category, index=self._index
        )

    def search(self, request: ErrorLexiconSearchRequest) -> ErrorLexiconSearchReply:
        """A page of this lexicon (see :func:`~mowdeck_contracts.v1.search_lexicon`); the
        requested codes are normalised first, so the mower's spelling (``-1201``) works."""
        codes = [self.code_of(code) for code in request.codes]
        return search_lexicon(
            self.lexicon, request.model_copy(update={"codes": codes}), index=self._index
        )

    def category_of(self, raw: str) -> MowerErrorCategory | None:
        """The category of ``raw``, None for an unknown code (adapters fill
        ``MowerState.error_category`` with it)."""
        entry = self.entry(raw)
        return entry.category if entry is not None else None


LexiconSource = (
    ErrorLexicon | ErrorLexiconProvider | Callable[[], ErrorLexicon | ErrorLexiconProvider]
)


def as_provider(source: LexiconSource) -> ErrorLexiconProvider:
    """``source`` (data, provider or factory) as a provider; raises ``TypeError`` otherwise."""
    value = source() if callable(source) and not isinstance(source, ErrorLexicon) else source
    if isinstance(value, ErrorLexiconProvider):
        return value
    if isinstance(value, ErrorLexicon):
        return ErrorLexiconProvider(value)
    msg = f"not an error lexicon: {type(value).__name__}"
    raise TypeError(msg)


def _installed_entry_points() -> Iterable[EntryPoint]:
    return entry_points(group=ENTRY_POINT_GROUP)


class ErrorLexiconRegistry:
    """Maps vendor keys to lexicons; loads the entry points at the first lookup."""

    def __init__(self, entry_point_source: EntryPointSource = _installed_entry_points) -> None:
        self._providers: dict[str, ErrorLexiconProvider] = {}
        self._entry_point_source = entry_point_source
        self._entry_points_loaded = False

    def register(self, source: LexiconSource, *, replace: bool = False) -> ErrorLexiconProvider:
        """Register a lexicon under its vendor key.

        Raises ``ValueError`` if another lexicon is registered for the vendor and ``replace`` is
        False.
        """
        provider = as_provider(source)
        existing = self._providers.get(provider.vendor)
        if existing is not None and existing.lexicon != provider.lexicon and not replace:
            msg = f"an error lexicon for '{provider.vendor}' is already registered"
            raise ValueError(msg)
        self._providers[provider.vendor] = provider
        return provider

    def load_entry_points(self) -> list[str]:
        """Register every lexicon of the ``mowdeck.error_lexicons`` entry points.

        An entry point that fails to import or build, gives no lexicon or names another vendor
        than its own name is logged and skipped; it never breaks the other lexicons. Returns the
        vendors that were registered.
        """
        self._entry_points_loaded = True
        loaded: list[str] = []
        for entry_point in self._entry_point_source():
            try:
                provider = as_provider(entry_point.load())
            except Exception:
                _log.exception("cannot load error lexicon entry point %s", entry_point)
                continue
            if provider.vendor != entry_point.name:
                _log.warning(
                    "ignoring error lexicon %s: it is for vendor %r",
                    entry_point,
                    provider.vendor,
                )
                continue
            if provider.vendor in self._providers:
                _log.info("keeping the registered error lexicon for %s", provider.vendor)
                continue
            self._providers[provider.vendor] = provider
            loaded.append(provider.vendor)
        return loaded

    def vendors(self) -> list[str]:
        """All registered vendors (entry points included), sorted."""
        self._ensure_entry_points()
        return sorted(self._providers)

    def infos(self) -> list[ErrorLexiconInfo]:
        """The summary of every registered lexicon, sorted by vendor."""
        return [self._providers[v].lexicon.info() for v in self.vendors()]

    def provider(
        self,
        vendor: str | None = None,
        connection: ConnectionKind | str | None = None,
        *,
        cloud_provider: str | None = None,
    ) -> ErrorLexiconProvider | None:
        """The lexicon to explain a mower's codes with; None when none applies.

        In order: the lexicon of ``vendor``; the first (by vendor) that serves ``cloud_provider``
        (:meth:`~mowdeck_contracts.v1.ErrorLexicon.serves_cloud_provider`), and none when no
        lexicon does: another vendor's lexicon on the same connection kind would explain the
        codes wrongly; the first that lists ``connection``.
        """
        self._ensure_entry_points()
        if vendor is not None:
            return self._providers.get(vendor)
        if cloud_provider:
            for key in sorted(self._providers):
                if self._providers[key].lexicon.serves_cloud_provider(cloud_provider):
                    return self._providers[key]
            return None
        if connection is None:
            return None
        for key in sorted(self._providers):
            provider = self._providers[key]
            if any(kind == connection for kind in provider.lexicon.connection_kinds):
                return provider
        return None

    def explain(
        self,
        codes: Sequence[str],
        locale: str | None = None,
        *,
        vendor: str | None = None,
        connection: ConnectionKind | str | None = None,
        cloud_provider: str | None = None,
        category: MowerErrorCategory | None = None,
    ) -> list[ErrorExplanation]:
        """Explain every code with the lexicon of ``vendor`` (else ``cloud_provider``, else
        ``connection``, see :meth:`provider`), in order."""
        provider = self.provider(vendor, connection, cloud_provider=cloud_provider)
        if provider is None:
            return [unknown_explanation(code, locale, category=category) for code in codes]
        return [provider.explain(code, locale, category=category) for code in codes]

    def search(self, request: ErrorLexiconSearchRequest) -> ErrorLexiconSearchReply:
        """Browse the lexicon of ``request.vendor`` (else ``request.cloud_provider``, else
        ``request.connection``); an empty reply (``lexicon`` null) when none applies."""
        provider = self.provider(
            request.vendor, request.connection, cloud_provider=request.cloud_provider
        )
        if provider is None:
            return ErrorLexiconSearchReply(offset=request.offset)
        return provider.search(request)

    def _ensure_entry_points(self) -> None:
        if not self._entry_points_loaded:
            self.load_entry_points()


_default_registry = ErrorLexiconRegistry()


def default_error_lexicons() -> ErrorLexiconRegistry:
    """The process wide registry of error lexicons."""
    return _default_registry


def register_error_lexicon(source: LexiconSource, *, replace: bool = False) -> None:
    """Register a lexicon in the default registry."""
    _default_registry.register(source, replace=replace)


def explain_error(
    code: str,
    locale: str | None = None,
    *,
    vendor: str | None = None,
    connection: ConnectionKind | str | None = None,
    cloud_provider: str | None = None,
    category: MowerErrorCategory | None = None,
) -> ErrorExplanation:
    """Explain one code with the default registry."""
    return _default_registry.explain(
        [code],
        locale,
        vendor=vendor,
        connection=connection,
        cloud_provider=cloud_provider,
        category=category,
    )[0]
