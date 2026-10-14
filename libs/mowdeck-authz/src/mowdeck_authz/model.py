"""The authorization model as data.

``infra/openfga/model.fga`` is the source of truth. The package ships its JSON form
(``model.json``), generated with the official CLI by ``uv run poe fga-json``; CI fails when it is
stale. Python code therefore needs no DSL parser.
"""

import json
from collections.abc import Mapping
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

_KEEP_EMPTY = frozenset({"this"})
"""``{"this": {}}`` means "directly assignable" and must survive normalisation."""


@cache
def _bundled_text() -> str:
    return resources.files("mowdeck_authz").joinpath("model.json").read_text(encoding="utf-8")


def load_model(path: str | Path | None = None) -> dict[str, Any]:
    """Return the model in OpenFGA JSON form (the packaged one unless ``path`` is given)."""
    text = Path(path).read_text(encoding="utf-8") if path is not None else _bundled_text()
    model: dict[str, Any] = json.loads(text)
    return model


def _normalise(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value):
            item = _normalise(value[key])
            if key in _KEEP_EMPTY or item not in (None, {}, [], ""):
                result[key] = item
        return result
    if isinstance(value, list | tuple):
        return [_normalise(item) for item in value]
    return value


def normalize_model(model: Mapping[str, Any]) -> dict[str, Any]:
    """A comparable form: no ``id``, sorted keys, empty and unset fields removed.

    The server returns empty metadata objects and default fields the CLI output omits; they do
    not change the meaning of the model.
    """
    stripped = {key: value for key, value in model.items() if key != "id"}
    normalised: dict[str, Any] = _normalise(stripped)
    return normalised


def models_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """True if both models are the same after :func:`normalize_model`."""
    return normalize_model(left) == normalize_model(right)
