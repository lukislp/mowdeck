# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The Stripe adapter's part of the erasure refund (ADR 0082, section 5): the requests it sends
(the idempotency key and the metadata included), the objects it reads back, what each refusal
becomes, and the same calls against stripe-mock (``MOWDECK_TEST_STRIPE_MOCK_URL``, see
``test_billing_stripe``). No real Stripe key and no real call.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
import stripe
from test_billing_stripe import MOCK_URL, _error, needs_mock

from billing.adapters.providers.fixtures import (
    charge_refunded_event,
    encode,
    refund_event,
    sign,
)
from billing.adapters.providers.stripe import PERMANENT_REFUND_CODES, StripeProvider
from billing.adapters.providers.stripe_events import line_from, refund_from
from billing.domain.errors import ProviderError, ProviderUnavailable, RefundImpossible
from billing.domain.models import InvoiceLine, LineKind, RefundStatus

START = 1_793_491_200  # 2026-11-01 00:00 UTC
END = 1_796_083_200  # 2026-12-01 00:00 UTC
METADATA = {
    "reason": "account_erasure",
    "account_id": "6f1c2a52-0d4e-4b8c-9a1e-3c5d7e9f1a2b",
    "subscription_id": "sub_1",
    "invoice_id": "in_2",
}


def _obj(value: dict[str, Any]) -> Any:
    return SimpleNamespace(to_dict=lambda: value, id=value.get("id"))


def _page(*values: dict[str, Any], has_more: bool = False) -> Any:
    return SimpleNamespace(data=[_obj(v) for v in values], has_more=has_more)


def _line(
    amount: int, parent: dict[str, Any], start: int = START, end: int = END
) -> dict[str, Any]:
    return {
        "id": f"il_{amount}",
        "object": "line_item",
        "amount": amount,
        "period": {"start": start, "end": end},
        "parent": parent,
    }


def _price(proration: bool = False) -> dict[str, Any]:
    return {
        "type": "subscription_item_details",
        "subscription_item_details": {"subscription_item": "si_1", "proration": proration},
    }


ONE_OFF = {"type": "invoice_item_details", "invoice_item_details": {"proration": False}}


class _Api:
    """The Stripe calls of the refund, answered or failed by the test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any]] = []
        self.invoices: list[list[dict[str, Any]]] = []
        self.lines: list[dict[str, Any]] = []
        self.payments: dict[str, list[dict[str, Any]]] = {}
        self.refunds: list[dict[str, Any]] = []
        self.create_error: stripe.StripeError | None = None
        self.list_error: stripe.StripeError | None = None
        self.status = "pending"
        self.line_items = SimpleNamespace(list_async=self._lines)

    async def list_async(self, params: Any = None, options: Any = None) -> Any:
        del options
        self.calls.append(("list", params, None))
        if self.list_error is not None:
            raise self.list_error
        if "subscription" in params:
            # Pages of invoices: the first, or the one after the invoice named by the cursor.
            index = 0
            if "starting_after" in params:
                index = next(
                    i + 1
                    for i, page in enumerate(self.invoices)
                    if page and page[-1]["id"] == params["starting_after"]
                )
            if index >= len(self.invoices):
                return _page()
            return _page(*self.invoices[index], has_more=index + 1 < len(self.invoices))
        if "invoice" in params:
            return _page(*self.payments.get(params["invoice"], []))
        return _page(*self.refunds)

    async def _lines(self, invoice_id: str, params: Any = None) -> Any:
        self.calls.append(("lines", invoice_id, params))
        return _page(*self.lines)

    async def create_async(self, params: Any = None, options: Any = None) -> Any:
        self.calls.append(("create", params, options))
        if self.create_error is not None:
            raise self.create_error
        refund = {
            "id": "re_1",
            "object": "refund",
            "amount": params["amount"],
            "currency": "eur",
            "payment_intent": params.get("payment_intent"),
            "charge": params.get("charge"),
            "metadata": params["metadata"],
            "status": self.status,
        }
        return _obj(refund)


def _with(api: _Api) -> StripeProvider:
    client = SimpleNamespace(v1=SimpleNamespace(invoices=api, invoice_payments=api, refunds=api))
    return StripeProvider("sk_test_123", "whsec", client=cast(stripe.StripeClient, client))


# --- reading the paid period ------------------------------------------------------------------


NOW = datetime(2026, 11, 16, tzinfo=UTC)


def _paid(
    invoice_id: str, amount: int, *lines: dict[str, Any], more: bool = False
) -> dict[str, Any]:
    return {
        "id": invoice_id,
        "status": "paid",
        "currency": "eur",
        "amount_paid": amount,
        "lines": {"data": list(lines), "has_more": more},
    }


def _by_intent(payment_intent: str, amount: int) -> dict[str, Any]:
    return {
        "id": f"inpay_{payment_intent}",
        "payment": {"type": "payment_intent", "payment_intent": payment_intent},
        "amount_paid": amount,
    }


async def test_billing_stripe_every_paid_invoice_that_covers_now() -> None:
    """Two pages of paid invoices, created at most one longest period back. Kept: an upgrade
    billed on its own (proration lines) and the month's invoice, whose first page of lines is
    completed from the lines endpoint and which was paid by a PaymentIntent. Passed over: last
    month's invoice and one of one-off items."""
    api = _Api()
    upgrade = _line(600, _price(proration=True), start=START + 1_296_000)
    credit = _line(-450, _price(proration=True), start=START + 1_296_000)
    api.invoices = [
        [_paid("in_3", 150, credit, upgrade), _paid("in_one_off", 200, _line(200, ONE_OFF))],
        [
            _paid("in_2", 1071, _line(900, _price()), more=True),
            _paid("in_1", 900, _line(900, _price(), start=START - 2_592_000, end=START)),
        ],
    ]
    api.lines = [
        _line(900, _price()),
        _line(200, ONE_OFF),
        _line(50, _price(), start=START - 2_592_000, end=START),
    ]
    api.payments = {
        "in_3": [_by_intent("pi_3", 150)],
        "in_2": [
            {"id": "inpay_0", "payment": {"type": "out_of_band_payment"}, "amount_paid": 1071},
            _by_intent("pi_2", 1071),
        ],
    }

    found = await _with(api).paid_invoices_covering("sub_1", NOW)

    assert [i.invoice_id for i in found] == ["in_3", "in_2"]
    upgrade_invoice, month = found
    assert (upgrade_invoice.payment_ref, upgrade_invoice.payment_amount) == ("pi_3", 150)
    assert [line.amount for line in upgrade_invoice.lines] == [-450, 600]
    assert {line.kind for line in upgrade_invoice.lines} == {LineKind.PRORATION}
    assert (month.subscription_id, month.status, month.currency) == ("sub_1", "paid", "eur")
    assert (month.amount_paid, month.payment_ref, month.payment_amount) == (1071, "pi_2", 1071)
    start, end = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)
    assert month.lines == (
        InvoiceLine(900, LineKind.RECURRING, start, end),
        InvoiceLine(200, LineKind.OTHER, start, end),
        InvoiceLine(50, LineKind.RECURRING, datetime(2026, 10, 2, tzinfo=UTC), start),
    )
    since = int((NOW - timedelta(days=366)).timestamp())
    first = {"subscription": "sub_1", "status": "paid", "created": {"gte": since}, "limit": 100}
    assert api.calls[0] == ("list", first, None)
    assert ("list", {**first, "starting_after": "in_one_off"}, None) in api.calls
    assert ("lines", "in_2", {"limit": 100}) in api.calls
    payments_asked = [c[1]["invoice"] for c in api.calls if "invoice" in (c[1] or {})]
    assert payments_asked == ["in_3", "in_2"]  # only for the invoices kept


async def test_billing_stripe_no_paid_invoice_and_one_paid_without_a_payment() -> None:
    api = _Api()
    assert await _with(api).paid_invoices_covering("sub_1", NOW) == []
    api.invoices = [[_paid("in_1", 0, _line(900, _price()))]]
    [found] = await _with(api).paid_invoices_covering("sub_1", NOW)
    assert (found.payment_ref, found.payment_amount) == (None, 0)


def test_billing_stripe_invoice_lines_say_what_they_bill() -> None:
    assert line_from(_line(1, _price())).kind is LineKind.RECURRING  # type: ignore[union-attr]
    assert line_from(_line(1, _price(True))).kind is LineKind.PRORATION  # type: ignore[union-attr]
    proration = {"type": "invoice_item_details", "invoice_item_details": {"proration": True}}
    assert line_from(_line(1, proration)).kind is LineKind.PRORATION  # type: ignore[union-attr]
    assert line_from(_line(1, ONE_OFF)).kind is LineKind.OTHER  # type: ignore[union-attr]
    assert line_from({"id": "il", "amount": 1}) is None


async def test_billing_stripe_refunds_of_a_payment_intent_or_a_charge() -> None:
    api = _Api()
    api.refunds = [
        {
            "id": "re_0",
            "amount": 300,
            "currency": "eur",
            "payment_intent": "pi_2",
            "status": "failed",
            "failure_reason": "expired_or_canceled_card",
            "metadata": METADATA,
        }
    ]
    [found] = await _with(api).refunds_of("pi_2")
    await _with(api).refunds_of("ch_2")

    assert (found.id, found.payment_ref, found.amount) == ("re_0", "pi_2", 300)
    assert (found.status, found.failure_reason) == (RefundStatus.FAILED, "expired_or_canceled_card")
    assert found.metadata == METADATA
    assert [c[1] for c in api.calls] == [
        {"payment_intent": "pi_2", "limit": 100},
        {"charge": "ch_2", "limit": 100},
    ]


# --- the refund -------------------------------------------------------------------------------


async def test_billing_stripe_refunds_with_the_key_and_the_metadata_of_the_erasure() -> None:
    api = _Api()

    made = await _with(api).refund("pi_2", 600, "eur", "mowdeck-erasure-refund-x", METADATA)

    assert api.calls == [
        (
            "create",
            {
                "payment_intent": "pi_2",
                "amount": 600,
                "reason": "requested_by_customer",
                "metadata": METADATA,
            },
            {"idempotency_key": "mowdeck-erasure-refund-x"},
        )
    ]
    assert (made.id, made.amount, made.status) == ("re_1", 600, RefundStatus.PENDING)
    assert made.metadata == METADATA


@pytest.mark.parametrize("code", sorted(PERMANENT_REFUND_CODES))
async def test_billing_stripe_a_refund_refused_for_good(code: str) -> None:
    api = _Api()
    api.create_error = _error(stripe.InvalidRequestError, "no", None, code=code, http_status=400)
    with pytest.raises(RefundImpossible, match=code):
        await _with(api).refund("pi_2", 600, "eur", "key", METADATA)


async def test_billing_stripe_any_invalid_refund_request_is_refused_for_good() -> None:
    """A payment older than Stripe refunds comes back as a 400 without a dedicated code: it
    must not block the erasure on every retry."""
    api = _Api()
    api.create_error = _error(
        stripe.InvalidRequestError, "charge too old to refund", None, http_status=400
    )
    with pytest.raises(RefundImpossible, match="too old"):
        await _with(api).refund("pi_2", 600, "eur", "key", METADATA)


@pytest.mark.parametrize(
    ("error", "later"),
    [
        (_error(stripe.RateLimitError, "slow down", http_status=429), True),
        (_error(stripe.APIConnectionError, "timed out"), True),
        (_error(stripe.APIError, "stripe is down", http_status=503), True),
        (_error(stripe.PermissionError, "restricted key", http_status=403), False),
    ],
)
async def test_billing_stripe_refund_failures_say_whether_a_retry_can_help(
    error: stripe.StripeError, later: bool
) -> None:
    api = _Api()
    api.create_error = error
    api.list_error = error
    for call in (
        _with(api).refund("pi_2", 600, "eur", "key", METADATA),
        _with(api).refunds_of("pi_2"),
        _with(api).paid_invoices_covering("sub_1", NOW),
    ):
        with pytest.raises(ProviderError) as failed:
            await call
        assert not isinstance(failed.value, RefundImpossible)
        assert isinstance(failed.value, ProviderUnavailable) is later


async def test_billing_stripe_a_key_reused_with_another_amount_is_asked_again_later() -> None:
    """The unused part shrank between two attempts, the key is the same: the next attempt finds
    the refund of the first by its metadata."""
    api = _Api()
    api.create_error = _error(stripe.IdempotencyError, "key reused with other parameters")
    with pytest.raises(ProviderUnavailable, match="idempotency key"):
        await _with(api).refund("pi_2", 550, "eur", "key", METADATA)


# --- refund events ----------------------------------------------------------------------------


def test_billing_stripe_refund_events_are_read() -> None:
    provider = StripeProvider("sk_test_123", "whsec_test")
    failed = refund_event(
        "refund.failed",
        refund_id="re_1",
        payment_intent="pi_2",
        amount=600,
        status="failed",
        metadata=METADATA,
        failure_reason="lost_or_stolen_card",
    )
    charge = charge_refunded_event(
        charge_id="ch_2",
        customer_id="cus_1",
        payment_intent="pi_2",
        amount=900,
        amount_refunded=600,
    )
    body = encode(failed)
    refund = provider.parse_webhook(body, sign(body, "whsec_test")).refund
    body = encode(charge)
    charged = provider.parse_webhook(body, sign(body, "whsec_test")).charge

    assert refund is not None
    assert (refund.status, refund.amount, refund.payment_ref) == (RefundStatus.FAILED, 600, "pi_2")
    assert refund.metadata == METADATA
    assert refund.failure_reason == "lost_or_stolen_card"
    assert charged is not None
    assert (charged.customer_id, charged.amount, charged.amount_refunded) == ("cus_1", 900, 600)
    assert refund_from({"id": "re_x", "status": "something new"}).status is RefundStatus.PENDING


# --- against stripe-mock ------------------------------------------------------------------------


@needs_mock
async def test_billing_stripe_mock_refund_calls() -> None:
    """stripe-mock checks every parameter against Stripe's API definition (the idempotency key
    is a header it accepts); its answers are fixtures, not the objects asked for."""
    provider = StripeProvider("sk_test_123", "whsec", api_base=MOCK_URL, max_network_retries=0)
    found = await provider.paid_invoices_covering("sub_123", datetime.now(UTC))
    assert all(invoice.subscription_id == "sub_123" for invoice in found)
    refunds = await provider.refunds_of("pi_123")
    assert all(r.id.startswith("re_") for r in refunds)
    await provider.refunds_of("ch_123")
    made = await provider.refund("pi_123", 600, "usd", "mowdeck-erasure-refund-test", METADATA)
    assert made.id.startswith("re_")
    assert made.amount == 600
    assert made.status in set(RefundStatus)
