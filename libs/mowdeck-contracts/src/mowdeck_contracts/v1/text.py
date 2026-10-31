"""Display text people type in: names of sites, groups, areas, zones, schedules and the like.

Such a name is shown to everybody who shares the account (and to every visitor of the public
demo, ADR 0068). Invisible characters would let one person spoof what the others read: a
right-to-left override (U+202E) turns ``Lawn`` around, a zero-width space (U+200B) makes two
names that look the same differ. :data:`PlainText` therefore cleans such text before any length
check:

* line breaks, tabs and the other control characters that separate text (``\\t``, ``\\n``,
  ``\\r``, U+0085, U+2028, U+2029) become a space;
* every other control character (Unicode category ``Cc``) and every format character (``Cf``:
  the bidirectional embeddings, overrides and isolates, zero-width spaces, the byte order mark,
  soft hyphens, invisible operators, tag characters) is removed;
* except the zero-width joiner and non-joiner (U+200D, U+200C): emoji sequences and several
  scripts (Persian, Indic) need them, and they reorder nothing.

Usage: ``name: PlainText = Field(min_length=1, max_length=100)``, or ``PlainText | None``.
"""

import unicodedata
from typing import Annotated, Final

from pydantic import BeforeValidator

KEPT_FORMAT_CHARACTERS: Final = frozenset({"\u200c", "\u200d"})
"""Format characters that stay: zero-width non-joiner and joiner."""

_SEPARATORS: Final = frozenset({"\t", "\n", "\v", "\f", "\r", "\x1c", "\x1d", "\x1e", "\x1f"})
_SEPARATORS_WIDE: Final = frozenset({"\x85", "\u2028", "\u2029"})


def clean_text(value: str) -> str:
    """``value`` without control and format characters (separators become spaces)."""
    if value.isascii() and value.isprintable():
        return value
    kept: list[str] = []
    for char in value:
        if char in _SEPARATORS or char in _SEPARATORS_WIDE:
            kept.append(" ")
        elif char in KEPT_FORMAT_CHARACTERS or unicodedata.category(char) not in ("Cc", "Cf"):
            kept.append(char)
    return "".join(kept)


def _clean(value: object) -> object:
    return clean_text(value) if isinstance(value, str) else value


PlainText = Annotated[str, BeforeValidator(_clean)]
"""A string a person typed for others to read, cleaned by :func:`clean_text` before the field's
own checks (``min_length`` counts what is left)."""
