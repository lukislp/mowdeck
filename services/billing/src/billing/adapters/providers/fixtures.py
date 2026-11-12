# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Stripe shaped webhook fixtures and their signatures, for tests and the local demo.

:func:`sign` builds a ``Stripe-Signature`` header exactly like Stripe (``t=<unix>,v1=<hex>``,
HMAC-SHA256 of ``"<t>.<raw body>"`` with the endpoint secret), so the same verification code
runs against fixtures, the Stripe CLI and real Stripe. The event builders follow the object
layout of the pinned API version (period on the subscription items, invoice subscription under
``parent.subscription_details``).
"""

import hashlib
import hmac
import json
import secrets
import time
from typing import Any
from uuid import UUID


def sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """The ``Stripe-Signature`` header value for ``payload``."""
    at = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{at}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={at},v1={digest}"


def _event(
    event_type: str, obj: dict[str, Any], created: int | None, event_id: str | None
) -> dict[str, Any]:
    return {
        "id": event_id or f"evt_{secrets.token_hex(12)}",
        "object": "event",
        "api_version": "2026-09-30.endive",
        "created": int(time.time()) if created is None else created,
        "livemode": False,
        "pending_webhooks": 1,
        "type": event_type,
        "data": {"object": obj},
    }


def subscription_event(
    event_type: str,
    *,
    subscription_id: str,
    customer_id: str,
    status: str,
    price_id: str,
    account_id: UUID | None = None,
    plan: str | None = None,
    period_start: int,
    period_end: int,
    trial_end: int | None = None,
    ended_at: int | None = None,
    cancel_at_period_end: bool = False,
    created: int | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """A ``customer.subscription.*`` event."""
    metadata: dict[str, str] = {}
    if account_id is not None:
        metadata["account_id"] = str(account_id)
    if plan is not None:
        metadata["plan"] = plan
    obj = {
        "id": subscription_id,
        "object": "subscription",
        "customer": customer_id,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "trial_end": trial_end,
        "ended_at": ended_at,
        "metadata": metadata,
        "items": {
            "object": "list",
            "data": [
                {
                    "id": f"si_{subscription_id[-8:]}",
                    "object": "subscription_item",
                    "current_period_start": period_start,
                    "current_period_end": period_end,
                    "price": {"id": price_id, "object": "price"},
                    "quantity": 1,
                }
            ],
        },
    }
    return _event(event_type, obj, created, event_id)


def invoice_event(
    event_type: str,
    *,
    invoice_id: str,
    customer_id: str,
    subscription_id: str,
    status: str,
    amount: int,
    currency: str = "eur",
    created: int | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """An ``invoice.paid`` or ``invoice.payment_failed`` event."""
    paid = status == "paid"
    obj = {
        "id": invoice_id,
        "object": "invoice",
        "customer": customer_id,
        "status": status,
        "currency": currency,
        "amount_due": amount,
        "amount_paid": amount if paid else 0,
        "created": int(time.time()) if created is None else created,
        "number": f"MD-{invoice_id[-6:].upper()}",
        "hosted_invoice_url": f"https://invoice.stripe.com/i/{invoice_id}",
        "invoice_pdf": f"https://pay.stripe.com/invoice/{invoice_id}/pdf",
        "parent": {
            "type": "subscription_details",
            "subscription_details": {"subscription": subscription_id, "metadata": {}},
        },
    }
    return _event(event_type, obj, created, event_id)


def checkout_completed_event(
    *,
    session_id: str,
    customer_id: str,
    subscription_id: str | None,
    account_id: UUID,
    plan: str,
    created: int | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """A ``checkout.session.completed`` event of a subscription checkout."""
    obj = {
        "id": session_id,
        "object": "checkout.session",
        "mode": "subscription",
        "status": "complete",
        "customer": customer_id,
        "subscription": subscription_id,
        "client_reference_id": str(account_id),
        "metadata": {"account_id": str(account_id), "plan": plan},
    }
    return _event("checkout.session.completed", obj, created, event_id)


def customer_event(
    event_type: str,
    *,
    customer_id: str,
    account_id: UUID | None = None,
    email: str | None = None,
    name: str | None = None,
    created: int | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """A ``customer.*`` event, e.g. the ``customer.deleted`` Stripe sends after a customer was
    deleted: the object is the customer as it was, with its e-mail address and name."""
    obj = {
        "id": customer_id,
        "object": "customer",
        "created": int(time.time()) if created is None else created,
        "email": email,
        "name": name,
        "livemode": False,
        "metadata": {"account_id": str(account_id)} if account_id is not None else {},
    }
    return _event(event_type, obj, created, event_id)


def refund_event(
    event_type: str,
    *,
    refund_id: str,
    payment_intent: str,
    amount: int,
    status: str,
    metadata: dict[str, str] | None = None,
    currency: str = "eur",
    failure_reason: str | None = None,
    created: int | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """A ``refund.updated`` or ``refund.failed`` event: the object is the refund, which names
    the payment but no customer."""
    obj = {
        "id": refund_id,
        "object": "refund",
        "amount": amount,
        "balance_transaction": None,
        "charge": f"ch_{payment_intent[-10:]}",
        "created": int(time.time()) if created is None else created,
        "currency": currency,
        "destination_details": {"type": "card", "card": {"type": "refund"}},
        "failure_reason": failure_reason,
        "metadata": metadata or {},
        "payment_intent": payment_intent,
        "reason": "requested_by_customer",
        "receipt_number": None,
        "status": status,
    }
    return _event(event_type, obj, created, event_id)


def charge_refunded_event(
    *,
    charge_id: str,
    customer_id: str,
    payment_intent: str,
    amount: int,
    amount_refunded: int,
    currency: str = "eur",
    created: int | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """A ``charge.refunded`` event: the object is the charge, with its customer and the billing
    details the customer gave."""
    obj = {
        "id": charge_id,
        "object": "charge",
        "amount": amount,
        "amount_captured": amount,
        "amount_refunded": amount_refunded,
        "billing_details": {"email": "lukas@example.org", "name": "Lukas Example"},
        "captured": True,
        "created": int(time.time()) if created is None else created,
        "currency": currency,
        "customer": customer_id,
        "paid": True,
        "payment_intent": payment_intent,
        "refunded": amount_refunded >= amount,
        "status": "succeeded",
    }
    return _event("charge.refunded", obj, created, event_id)


def encode(event: dict[str, Any]) -> bytes:
    """The raw body Stripe would send."""
    return json.dumps(event, separators=(",", ":")).encode()
