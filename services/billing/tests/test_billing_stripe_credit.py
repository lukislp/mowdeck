# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The Stripe adapter's part of paying back a credit balance on an erasure (ADR 0082, section
5): the requests it sends (the idempotency key and the metadata included), the objects it reads
back, what each refusal becomes, and the same calls against stripe-mock
(``MOWDECK_TEST_STRIPE_MOCK_URL``, see ``test_billing_stripe``). No real Stripe key and no real
call.
"""

from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
import stripe
from test_billing_stripe import MOCK_URL, _error, needs_mock

from billing.adapters.providers.stripe import StripeProvider
from billing.adapters.providers.stripe_events import credit_balance_from, recent_payment_from
from billing.domain.credit import clearing_metadata
from billing.domain.errors import ProviderError, ProviderUnavailable
from billing.domain.models import CreditBalance, RecentPayment

ACCOUNT = UUID("6f1c2a52-0d4e-4b8c-9a1e-3c5d7e9f1a2b")
NOW = datetime(2026, 11, 16, tzinfo=UTC)
TS = int(NOW.timestamp())


def _obj(value: dict[str, Any]) -> Any:
    return SimpleNamespace(to_dict=lambda: value, id=value.get("id"))


def _page(*values: dict[str, Any], has_more: bool = False) -> Any:
    return SimpleNamespace(data=[_obj(v) for v in values], has_more=has_more)


class _Api:
    """``customers``, ``customers.balance_transactions`` and ``charges``, answered by the test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any, Any]] = []
        self.customer: dict[str, Any] = {"id": "cus_1", "balance": -2500, "currency": "eur"}
        self.transactions: list[list[dict[str, Any]]] = [[]]
        self.charges: list[list[dict[str, Any]]] = [[]]
        self.error: stripe.StripeError | None = None
        self.balance_transactions = SimpleNamespace(
            list_async=self._list_transactions, create_async=self._create_transaction
        )

    def _fail(self) -> None:
        if self.error is not None:
            raise self.error

    async def retrieve_async(self, customer_id: str, params: Any = None) -> Any:
        self.calls.append(("retrieve", customer_id, params, None))
        self._fail()
        return _obj(self.customer)

    async def _list_transactions(self, customer_id: str, params: Any = None) -> Any:
        self.calls.append(("transactions", customer_id, params, None))
        index = 0 if "starting_after" not in params else 1
        return _page(*self.transactions[index], has_more=index + 1 < len(self.transactions))

    async def _create_transaction(
        self, customer_id: str, params: Any = None, options: Any = None
    ) -> Any:
        self.calls.append(("clear", customer_id, params, options))
        self._fail()
        return _obj({"id": "cbtxn_1", "amount": params["amount"]})

    async def list_async(self, params: Any = None, options: Any = None) -> Any:
        del options
        self.calls.append(("charges", None, params, None))
        self._fail()
        index = 0 if "starting_after" not in params else 1
        return _page(*self.charges[index], has_more=index + 1 < len(self.charges))


def _with(api: _Api) -> StripeProvider:
    client = SimpleNamespace(v1=SimpleNamespace(customers=api, charges=api))
    return StripeProvider("sk_test_123", "whsec", client=cast(stripe.StripeClient, client))


def _charge(charge_id: str, days_ago: int, **values: Any) -> dict[str, Any]:
    return {
        "id": charge_id,
        "object": "charge",
        "amount": 900,
        "amount_captured": 900,
        "amount_refunded": 0,
        "currency": "eur",
        "status": "succeeded",
        "paid": True,
        "payment_intent": f"pi_{charge_id[3:]}",
        "created": TS - days_ago * 86_400,
        **values,
    }


# --- the balance ------------------------------------------------------------------------------


async def test_billing_stripe_credit_balance_with_what_an_erasure_cleared() -> None:
    """Stripe's negative balance is a credit. Two pages of balance transactions: the erasure's
    debit (metadata) counts as cleared, another debit and a credit do not."""
    api = _Api()
    api.customer = {"id": "cus_1", "balance": -700, "currency": "eur"}
    ours = clearing_metadata(ACCOUNT)
    api.transactions = [
        [
            {"id": "cbtxn_3", "amount": 1800, "metadata": ours},
            {"id": "cbtxn_2", "amount": 300, "metadata": {}},
        ],
        [{"id": "cbtxn_1", "amount": -2500, "metadata": {}}],
    ]

    found = await _with(api).customer_credit_balance("cus_1")

    assert found == CreditBalance(700, "eur", 1800)
    assert api.calls == [
        ("retrieve", "cus_1", None, None),
        ("transactions", "cus_1", {"limit": 100}, None),
        ("transactions", "cus_1", {"limit": 100, "starting_after": "cbtxn_2"}, None),
    ]


async def test_billing_stripe_a_deleted_or_unknown_customer_has_no_balance() -> None:
    api = _Api()
    api.customer = {"id": "cus_1", "deleted": True}
    assert await _with(api).customer_credit_balance("cus_1") is None
    api.error = _error(
        stripe.InvalidRequestError, "No such customer", None, code="resource_missing"
    )
    assert await _with(api).customer_credit_balance("cus_1") is None


def test_billing_stripe_balances_are_read() -> None:
    assert credit_balance_from({"balance": -2500, "currency": "eur"}) == CreditBalance(2500, "eur")
    assert credit_balance_from({"balance": 1200, "currency": "eur"}).amount == -1200
    assert credit_balance_from({"balance": 0, "currency": None}) == CreditBalance(0, "")


# --- the payments -----------------------------------------------------------------------------


async def test_billing_stripe_recent_payments_newest_first_every_page() -> None:
    api = _Api()
    api.charges = [
        [
            _charge("ch_nov", 16, amount_refunded=600),
            _charge("ch_failed", 15, status="failed", paid=False),
        ],
        [
            _charge("ch_sep", 76),
            _charge("ch_oct", 46, payment_intent=None, amount_captured=850),
        ],
    ]
    since = NOW - timedelta(days=366)

    found = await _with(api).recent_payments("cus_1", since)

    assert found == [
        RecentPayment("pi_nov", 900, "eur", NOW - timedelta(days=16), 600),
        RecentPayment("ch_oct", 850, "eur", NOW - timedelta(days=46), 0),
        RecentPayment("pi_sep", 900, "eur", NOW - timedelta(days=76), 0),
    ]
    first = {"customer": "cus_1", "created": {"gte": int(since.timestamp())}, "limit": 100}
    assert [c[2] for c in api.calls] == [first, {**first, "starting_after": "ch_failed"}]


def test_billing_stripe_a_charge_that_did_not_succeed_is_no_payment() -> None:
    assert recent_payment_from(_charge("ch_1", 1, status="pending")) is None
    assert recent_payment_from(_charge("ch_1", 1, paid=False)) is None
    assert recent_payment_from(_charge("ch_1", 1, created=None)) is None


# --- zeroing the balance ----------------------------------------------------------------------


async def test_billing_stripe_the_balance_is_zeroed_with_a_debit_under_the_key() -> None:
    api = _Api()
    metadata = clearing_metadata(ACCOUNT)

    await _with(api).clear_credit_balance(
        "cus_1", 2500, "EUR", "mowdeck-erasure-credit-x", metadata
    )

    assert api.calls == [
        (
            "clear",
            "cus_1",
            {
                "amount": 2500,
                "currency": "eur",
                "description": "Credit balance paid back on account erasure",
                "metadata": metadata,
            },
            {"idempotency_key": "mowdeck-erasure-credit-x"},
        )
    ]


async def test_billing_stripe_zeroing_a_deleted_customer_is_done() -> None:
    api = _Api()
    api.error = _error(
        stripe.InvalidRequestError, "No such customer", None, code="resource_missing"
    )
    await _with(api).clear_credit_balance("cus_1", 100, "eur", "key", {})


@pytest.mark.parametrize(
    ("error", "later"),
    [
        (_error(stripe.RateLimitError, "slow down", http_status=429), True),
        (_error(stripe.APIConnectionError, "timed out"), True),
        (_error(stripe.IdempotencyError, "key reused with other parameters"), True),
        (_error(stripe.PermissionError, "restricted key", http_status=403), False),
        (_error(stripe.InvalidRequestError, "bad currency", None, http_status=400), False),
    ],
)
async def test_billing_stripe_credit_failures_say_whether_a_retry_can_help(
    error: stripe.StripeError, later: bool
) -> None:
    api = _Api()
    api.error = error
    calls: list[Coroutine[Any, Any, object]] = [
        _with(api).clear_credit_balance("cus_1", 100, "eur", "key", {})
    ]
    if not isinstance(error, stripe.IdempotencyError | stripe.InvalidRequestError):
        calls += [
            _with(api).customer_credit_balance("cus_1"),
            _with(api).recent_payments("cus_1", NOW),
        ]
    for call in calls:
        with pytest.raises(ProviderError) as failed:
            await call
        assert isinstance(failed.value, ProviderUnavailable) is later


# --- against stripe-mock ------------------------------------------------------------------------


@needs_mock
async def test_billing_stripe_mock_credit_balance_calls() -> None:
    """stripe-mock checks every parameter against Stripe's API definition; its answers are
    fixtures, not the objects asked for."""
    provider = StripeProvider("sk_test_123", "whsec", api_base=MOCK_URL, max_network_retries=0)
    balance = await provider.customer_credit_balance("cus_123")
    assert balance is not None
    payments = await provider.recent_payments("cus_123", datetime.now(UTC) - timedelta(days=366))
    assert all(p.payment_ref.startswith(("pi_", "ch_")) for p in payments)
    await provider.clear_credit_balance(
        "cus_123", 600, "usd", "mowdeck-erasure-credit-clear-test", clearing_metadata(ACCOUNT)
    )
