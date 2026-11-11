# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The rules of erasing an account in Billing (ADR 0082), free of storage and provider."""

from collections.abc import Iterable

LOCAL_PROVIDERS = frozenset({"fake"})
"""Providers that keep nothing outside MowDeck: the fake of tests and demos charges nobody and
stores nothing anywhere. Their rows are deleted without asking anybody."""


def unreachable_providers(stored: Iterable[str], configured: str) -> list[str]:
    """The providers an account still has something at that this installation cannot ask to
    delete it: neither the configured provider nor a local one. An erasure must not confirm
    while there is one (an installation that changed its provider has to finish the accounts
    of the old one with the old one)."""
    return sorted(set(stored) - {configured} - LOCAL_PROVIDERS)
