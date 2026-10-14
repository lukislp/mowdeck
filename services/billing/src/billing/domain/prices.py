"""The price file ``infra/billing/prices.yaml``: which provider price each plan is sold at.

::

    currency: eur
    prices:
      home: price_1Q...      # Stripe price id of the plan "home"; empty: not sold
      fleet: ""

``MOWDECK_BILLING_PRICE_IDS`` (``home=price_1,fleet=price_2``) overrides single entries, so the
ids of the operator's Stripe account need no change of the file.
"""

import re
from dataclasses import dataclass, field

import yaml

from billing.domain.errors import InvalidInput

_PLAN_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PRICE_ID = re.compile(r"^[A-Za-z0-9_]{1,255}$")


@dataclass(frozen=True, slots=True)
class PriceTable:
    """Plan key to provider price id, plus the display currency."""

    currency: str = "eur"
    prices: dict[str, str] = field(default_factory=dict)

    def price_for(self, plan_key: str) -> str | None:
        """The price id of a plan, or None when the plan is not sold."""
        return self.prices.get(plan_key) or None

    def plan_for(self, price_id: str | None) -> str | None:
        """The plan sold at ``price_id`` (reverse lookup), or None."""
        if not price_id:
            return None
        for plan, price in self.prices.items():
            if price == price_id:
                return plan
        return None

    @property
    def sold(self) -> dict[str, str]:
        """The plans that have a price id, in file order."""
        return {plan: price for plan, price in self.prices.items() if price}

    def with_overrides(self, overrides: str) -> "PriceTable":
        """Apply ``home=price_1,fleet=price_2`` on top of the file."""
        prices = dict(self.prices)
        prices.update(parse_overrides(overrides))
        return PriceTable(currency=self.currency, prices=prices)


def _check(plan: object, price: object) -> tuple[str, str]:
    if not isinstance(plan, str) or not _PLAN_KEY.match(plan):
        raise InvalidInput(f"invalid plan key {plan!r} in the price file")
    if price is None or price == "":
        return plan, ""
    if not isinstance(price, str) or not _PRICE_ID.match(price):
        raise InvalidInput(f"invalid price id for plan {plan!r}")
    return plan, price


def parse_price_file(text: str) -> PriceTable:
    """Parse the price file (``InvalidInput`` with a readable message when invalid)."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise InvalidInput(f"the price file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidInput("the price file must be a mapping with 'prices'")
    currency = data.get("currency", "eur")
    if not isinstance(currency, str) or not re.fullmatch(r"[a-z]{3}", currency):
        raise InvalidInput("currency must be a lower case ISO 4217 code such as eur")
    raw = data.get("prices") or {}
    if not isinstance(raw, dict):
        raise InvalidInput("prices must map plan keys to price ids")
    prices = dict(_check(plan, price) for plan, price in raw.items())
    return PriceTable(currency=currency, prices=prices)


def parse_overrides(overrides: str) -> dict[str, str]:
    """``home=price_1, fleet=price_2`` as a mapping (``InvalidInput`` when malformed)."""
    result: dict[str, str] = {}
    for part in overrides.split(","):
        item = part.strip()
        if not item:
            continue
        plan, sep, price = item.partition("=")
        if not sep:
            raise InvalidInput(f"MOWDECK_BILLING_PRICE_IDS: expected plan=price_id, got {item!r}")
        key, value = _check(plan.strip(), price.strip())
        result[key] = value
    return result
