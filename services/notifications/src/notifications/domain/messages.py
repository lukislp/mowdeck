"""Rendering of message keys into localized text.

The templates are data (``notifications/messages/<language>.json``, one object of message key
to ``str.format`` template per language). A missing key falls back to the default language,
then to the key itself; a missing parameter renders as an empty string, so a template never
raises.
"""

import string
from collections.abc import Mapping
from typing import Any

DEFAULT_LANGUAGE = "en"


class _Lenient(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""


class MessageCatalog:
    """Templates per language."""

    def __init__(self, templates: Mapping[str, Mapping[str, str]]) -> None:
        if DEFAULT_LANGUAGE not in templates:
            msg = f"the catalog needs the default language {DEFAULT_LANGUAGE!r}"
            raise ValueError(msg)
        self._templates = {lang: dict(values) for lang, values in templates.items()}

    @property
    def languages(self) -> list[str]:
        """The languages with templates, sorted."""
        return sorted(self._templates)

    def keys(self, language: str) -> set[str]:
        """The message keys of ``language``."""
        return set(self._templates.get(language, {}))

    def template(self, language: str, key: str) -> str:
        """The template of ``key`` in ``language`` (with fallbacks)."""
        for lang in (language, DEFAULT_LANGUAGE):
            value = self._templates.get(lang, {}).get(key)
            if value is not None:
                return value
        return key

    def render(self, language: str, key: str, params: Mapping[str, Any]) -> str:
        """Render ``key`` with ``params``; unknown placeholders become empty."""
        template = self.template(language, key)
        try:
            text = string.Formatter().vformat(template, (), _Lenient(params))
        except (ValueError, IndexError, AttributeError, KeyError):
            return template
        return " ".join(text.split())
