# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""How many decimals an amount of a currency has, in the provider's smallest unit.

Stripe writes every amount as an integer in the currency's smallest unit: cents for EUR (``990``
is 9.90 EUR), whole units for a zero-decimal currency (``1000`` JPY is 1000 yen), and
thousandths for the three-decimal currencies (``1000`` KWD is 1.000 KWD). ISK and UGX are
zero-decimal currencies that Stripe still writes with two decimals for backwards compatibility,
so they divide by 100 like EUR (and are refunded in whole units only, see
:data:`billing.domain.refunds.SMALLEST_STEP`).

The operator dashboard sends the exponent with every currency it shows, so the web app formats
amounts with these rules and keeps no list of its own.
"""

ZERO_DECIMAL = frozenset(
    {
        "bif",
        "clp",
        "djf",
        "gnf",
        "jpy",
        "kmf",
        "krw",
        "mga",
        "pyg",
        "rwf",
        "vnd",
        "vuv",
        "xaf",
        "xof",
        "xpf",
    }
)
"""Stripe's zero-decimal currencies: the amount is in whole units."""

THREE_DECIMAL = frozenset({"bhd", "jod", "kwd", "omr", "tnd"})
"""Stripe's three-decimal currencies: the amount is in thousandths."""


def minor_unit_exponent(currency: str) -> int:
    """The power of ten between one unit of ``currency`` and its provider amount (2 for EUR:
    ``990`` is 9.90; 0 for JPY; 3 for KWD)."""
    code = currency.lower()
    if code in ZERO_DECIMAL:
        return 0
    if code in THREE_DECIMAL:
        return 3
    return 2
