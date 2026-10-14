"""Column codecs: domain values the PostgreSQL store keeps as JSON.

The domain holds typed values; only this adapter knows their stored shape. A codec encodes a
domain value into the column value before a write and decodes it after a read, so a change of
the shape stays here (the memory store keeps the domain values themselves).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mowdeck_contracts.v1 import MowPattern
from pydantic import ValidationError

from fleet.domain.models import MowDefaults


@dataclass(frozen=True, slots=True)
class Codec:
    """Encode a domain value into a column value, decode a column value into the domain."""

    encode: Callable[[Any], Any]
    decode: Callable[[Any], Any]


def encode_mow_defaults(value: MowDefaults | None) -> dict[str, Any] | None:
    """``mowers.mow_defaults``: ``{"pattern": MowPattern}`` as JSON without unset optional
    keys (no ``spacing_m`` while it is None, so a model default stays the mower's own); NULL
    for None."""
    if value is None:
        return None
    return {"pattern": value.pattern.model_dump(mode="json", exclude_none=True)}


def decode_mow_defaults(raw: Any) -> MowDefaults | None:
    """The stored defaults; None for NULL and for a row this version cannot read (a pattern of
    a newer version, a hand-edited row): clients then fall back to their own defaults."""
    pattern = raw.get("pattern") if isinstance(raw, Mapping) else None
    if not isinstance(pattern, Mapping):
        return None
    try:
        return MowDefaults(pattern=MowPattern.model_validate(pattern))
    except ValidationError:
        return None


MOWER_CODECS: Mapping[str, Codec] = {
    "mow_defaults": Codec(encode_mow_defaults, decode_mow_defaults),
}
"""Codecs of the ``mowers`` table by column."""
