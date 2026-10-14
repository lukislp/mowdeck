"""Message templates (data files, one per language) and their loader."""

import json
from functools import cache
from importlib import resources

from notifications.domain.messages import MessageCatalog

LANGUAGES = ("de", "en")
"""Languages with templates in this package."""


@cache
def load_catalog() -> MessageCatalog:
    """The packaged catalog (``de.json``, ``en.json``), loaded once."""
    package = resources.files(__name__)
    templates = {
        lang: json.loads(package.joinpath(f"{lang}.json").read_text("utf-8")) for lang in LANGUAGES
    }
    return MessageCatalog(templates)
