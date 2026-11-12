# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Translate Stripe objects (plain dicts of a webhook payload or an API answer) into the provider
independent models of ``billing.domain.models``.

Field paths follow the Stripe API version pinned by the SDK (``2026-09-30.endive``): since
``2025-03-31.basil`` the billing period lives on the subscription items
(``items.data[].current_period_end``) and an invoice names its subscription under
``parent.subscription_details.subscription``. The older top-level fields are read as a fallback.
An invoice line says what it bills under ``parent``: ``subscription_item_details`` (a price of
the subscription, or a proration of it) or ``invoice_item_details`` (a one-off item, or a
proration); an invoice names its payments in ``invoice_payments``, not on the invoice.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import SubscriptionStatus

from billing.domain.models import (
    CreditBalance,
    InvoiceLine,
    LineKind,
    PaidInvoice,
    ProviderCharge,
    ProviderCheckout,
    ProviderEvent,
    ProviderInvoice,
    ProviderRefund,
    ProviderSubscription,
    RecentPayment,
    RefundStatus,
)


def _ts(value: Any) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC)
    return None


def _id(value: Any) -> str | None:
    """An id field that may be expanded into an object."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        return str(value["id"])
    return None


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def _metadata(obj: dict[str, Any]) -> dict[str, Any]:
    metadata = obj.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _first_item(obj: dict[str, Any]) -> dict[str, Any]:
    items = obj.get("items")
    data = items.get("data") if isinstance(items, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def subscription_from(obj: dict[str, Any]) -> ProviderSubscription:
    """A Stripe ``subscription`` object."""
    item = _first_item(obj)
    price = item.get("price")
    metadata = _metadata(obj)
    try:
        status = SubscriptionStatus(str(obj.get("status")))
    except ValueError:
        status = SubscriptionStatus.INCOMPLETE
    return ProviderSubscription(
        id=str(obj["id"]),
        customer_id=_id(obj.get("customer")),
        status=status,
        price_id=_id(price),
        plan_key=metadata.get("plan") or None,
        account_id=_uuid(metadata.get("account_id")),
        current_period_start=_ts(item.get("current_period_start"))
        or _ts(obj.get("current_period_start")),
        current_period_end=_ts(item.get("current_period_end"))
        or _ts(obj.get("current_period_end")),
        ended_at=_ts(obj.get("ended_at")),
        trial_end=_ts(obj.get("trial_end")),
        cancel_at_period_end=bool(obj.get("cancel_at_period_end")),
    )


def invoice_from(obj: dict[str, Any]) -> ProviderInvoice:
    """A Stripe ``invoice`` object."""
    parent = obj.get("parent") if isinstance(obj.get("parent"), dict) else {}
    details = parent.get("subscription_details") if isinstance(parent, dict) else None
    subscription = _id(details.get("subscription")) if isinstance(details, dict) else None
    return ProviderInvoice(
        id=str(obj["id"]),
        customer_id=_id(obj.get("customer")),
        status=str(obj.get("status") or "unknown"),
        currency=str(obj.get("currency") or ""),
        amount_due=int(obj.get("amount_due") or 0),
        amount_paid=int(obj.get("amount_paid") or 0),
        created=_ts(obj.get("created")) or datetime.now(UTC),
        subscription_id=subscription or _id(obj.get("subscription")),
        number=obj.get("number") or None,
        hosted_url=obj.get("hosted_invoice_url") or None,
        pdf_url=obj.get("invoice_pdf") or None,
    )


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def line_from(obj: dict[str, Any]) -> InvoiceLine | None:
    """A Stripe invoice ``line_item``; None without a period."""
    period = obj.get("period") if isinstance(obj.get("period"), dict) else {}
    start = _ts(period.get("start")) if period else None
    end = _ts(period.get("end")) if period else None
    if start is None or end is None:
        return None
    parent = obj.get("parent") if isinstance(obj.get("parent"), dict) else {}
    details = parent.get("subscription_item_details") if parent else None
    one_off = parent.get("invoice_item_details") if parent else None
    if isinstance(details, dict):
        kind = LineKind.PRORATION if details.get("proration") else LineKind.RECURRING
    elif isinstance(one_off, dict) and one_off.get("proration"):
        kind = LineKind.PRORATION
    else:
        kind = LineKind.OTHER
    return InvoiceLine(
        amount=_int(obj.get("amount")), kind=kind, period_start=start, period_end=end
    )


def payment_ref_from(obj: dict[str, Any]) -> str | None:
    """The PaymentIntent or charge of a Stripe ``invoice_payment``."""
    payment = obj.get("payment") if isinstance(obj.get("payment"), dict) else {}
    if not payment:
        return None
    return _id(payment.get("payment_intent")) or _id(payment.get("charge"))


def paid_invoice_from(
    obj: dict[str, Any],
    subscription_id: str,
    lines: list[dict[str, Any]],
    payment: dict[str, Any] | None,
) -> PaidInvoice:
    """A paid Stripe ``invoice`` with all its lines and the ``invoice_payment`` that paid it."""
    found = [line_from(line) for line in lines]
    return PaidInvoice(
        invoice_id=str(obj["id"]),
        subscription_id=subscription_id,
        status=str(obj.get("status") or "unknown"),
        currency=str(obj.get("currency") or ""),
        amount_paid=_int(obj.get("amount_paid")),
        lines=tuple(line for line in found if line is not None),
        payment_ref=payment_ref_from(payment) if payment else None,
        payment_amount=_int(payment.get("amount_paid")) if payment else 0,
    )


def refund_from(obj: dict[str, Any]) -> ProviderRefund:
    """A Stripe ``refund``."""
    try:
        status = RefundStatus(str(obj.get("status")))
    except ValueError:
        status = RefundStatus.PENDING
    metadata = {str(k): str(v) for k, v in _metadata(obj).items()}
    return ProviderRefund(
        id=str(obj["id"]),
        payment_ref=_id(obj.get("payment_intent")) or _id(obj.get("charge")),
        amount=_int(obj.get("amount")),
        currency=str(obj.get("currency") or ""),
        status=status,
        metadata=metadata,
        failure_reason=obj.get("failure_reason") or None,
    )


def credit_balance_from(obj: dict[str, Any], cleared: int = 0) -> CreditBalance:
    """The balance of a Stripe ``customer``: Stripe's ``balance`` is negative for a credit, the
    model's ``amount`` positive."""
    return CreditBalance(
        amount=-_int(obj.get("balance")),
        currency=str(obj.get("currency") or ""),
        cleared=cleared,
    )


def recent_payment_from(obj: dict[str, Any]) -> RecentPayment | None:
    """A successful Stripe ``charge`` as a payment to refund against (its PaymentIntent when it
    has one, as the refunds of an invoice name it); None for a charge that did not succeed."""
    if obj.get("status") != "succeeded" or obj.get("paid") is False:
        return None
    created = _ts(obj.get("created"))
    if created is None:
        return None
    captured = obj.get("amount_captured")
    amount = captured if isinstance(captured, int) and captured > 0 else _int(obj.get("amount"))
    return RecentPayment(
        payment_ref=_id(obj.get("payment_intent")) or str(obj["id"]),
        amount=amount,
        currency=str(obj.get("currency") or ""),
        created=created,
        refunded=_int(obj.get("amount_refunded")),
    )


def charge_from(obj: dict[str, Any]) -> ProviderCharge:
    """A Stripe ``charge``."""
    return ProviderCharge(
        id=str(obj["id"]),
        customer_id=_id(obj.get("customer")),
        currency=str(obj.get("currency") or ""),
        amount=_int(obj.get("amount")),
        amount_refunded=_int(obj.get("amount_refunded")),
    )


def checkout_from(obj: dict[str, Any]) -> ProviderCheckout:
    """A Stripe ``checkout.session`` object."""
    metadata = _metadata(obj)
    return ProviderCheckout(
        id=str(obj["id"]),
        customer_id=_id(obj.get("customer")),
        subscription_id=_id(obj.get("subscription")),
        account_id=_uuid(obj.get("client_reference_id")) or _uuid(metadata.get("account_id")),
        plan_key=metadata.get("plan") or None,
    )


def event_from(payload: dict[str, Any]) -> ProviderEvent:
    """A Stripe ``event`` (snapshot payload) with the object it carries."""
    data = payload.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    obj = obj if isinstance(obj, dict) else {}
    kind = obj.get("object")
    return ProviderEvent(
        id=str(payload["id"]),
        type=str(payload.get("type") or ""),
        created=_ts(payload.get("created")) or datetime.now(UTC),
        subscription=subscription_from(obj) if kind == "subscription" else None,
        invoice=invoice_from(obj) if kind == "invoice" else None,
        checkout=checkout_from(obj) if kind == "checkout.session" else None,
        refund=refund_from(obj) if kind == "refund" else None,
        charge=charge_from(obj) if kind == "charge" else None,
        livemode=bool(payload.get("livemode")),
    )
