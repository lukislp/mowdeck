"""Every shipped cloud provider speaks the app's languages (ADR 0066).

The web app is German and English; a provider's user-visible texts (summary, setup steps,
links, field labels and help, choice labels, mower option titles and help) come from its
descriptor, so each of them must exist in both. The check creates every installed provider's
plugin on its fake cloud (no network) and reads the descriptor.
"""

import asyncio

import pytest
from mowdeck_control import ProviderContext
from mowdeck_control.providers import installed_specs

APP_LOCALES = ("en", "de")
"""The languages of the web app (``apps/web/messages``)."""

SPECS = installed_specs()


def test_providers_are_installed() -> None:
    assert SPECS, "no cloud provider is installed in the workspace"


@pytest.mark.parametrize("provider", sorted(SPECS))
def test_provider_texts_exist_in_every_app_language(provider: str) -> None:
    plugin = SPECS[provider].create(ProviderContext(fake=True))
    try:
        missing = [
            f"{where}: {locale}"
            for where, text in plugin.descriptor.texts()
            for locale in APP_LOCALES
            if not text.get(locale, "").strip()
        ]
    finally:
        asyncio.run(plugin.close())
    assert not missing, f"{provider} lacks texts:\n" + "\n".join(missing)
