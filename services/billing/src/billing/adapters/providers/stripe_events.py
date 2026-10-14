"""Translate Stripe objects (plain dicts of a webhook payload or an API answer) into the provider
independent models of ``billing.domain.models``.

Field paths follow the Stripe API version pinned by the SDK (``2026-09-30.endive``): since
``2025-03-31.basil`` the billing period lives on the subscription items
(``items.data[].current_period_end``) and an invoice names its subscription under
``parent.subscription_details.subscription``. The older top-level fields are read as a fallback.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import SubscriptionStatus

from billing.domain.models import (
    ProviderCheckout,
    ProviderEvent,
    ProviderInvoice,
    ProviderSubscription,
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
        livemode=bool(payload.get("livemode")),
    )
