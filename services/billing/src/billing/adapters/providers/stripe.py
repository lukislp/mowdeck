# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Stripe adapter: Checkout Sessions (subscription mode), the customer portal, webhooks.

Calls go through ``stripe.StripeClient`` (async methods over httpx). ``api_base`` points the
client at stripe-mock for tests (``http://127.0.0.1:12111``); stripe-mock validates every
request against Stripe's OpenAPI document but keeps no state and sends no webhooks.

* One customer per account, created with ``metadata.account_id`` (idempotency key per account).
* Checkout: ``mode=subscription``, ``client_reference_id`` and ``metadata`` carry the account id
  and the plan, ``subscription_data.metadata`` too (so every subscription event names them),
  ``subscription_data.trial_period_days`` for a first subscription, ``automatic_tax`` when
  Stripe Tax is enabled. Business accounts (and accounts of unknown kind) give a tax id and a
  billing address (``tax_id_collection``, ``billing_address_collection=required``); with an
  existing customer ``customer_update`` saves name and address entered in Checkout.
* Webhooks: the ``Stripe-Signature`` header is verified with the endpoint secret
  (``stripe.WebhookSignature``, 300 s tolerance) against the raw body before anything is parsed.
* Account erasure (ADR 0082): first the refund of the unused part of the paid period. The
  latest paid invoice of the subscription is ``GET /v1/invoices?subscription=&status=paid``
  (the newest one with a line of a subscription price; all its lines from
  ``GET /v1/invoices/{id}/lines`` when the invoice carries only the first page), its payment
  ``GET /v1/invoice_payments?invoice=&status=paid``, the refunds of that payment
  ``GET /v1/refunds?payment_intent=`` (or ``charge=``). The refund is ``POST /v1/refunds`` on
  the PaymentIntent (or charge) with ``amount``, ``reason=requested_by_customer``, the metadata
  of the erasure and an ``Idempotency-Key`` header; Stripe refunds in the payment's currency to
  the original payment method. A refusal with one of :data:`PERMANENT_REFUND_CODES` is
  ``RefundImpossible``; a reused key with other parameters (``IdempotencyError``: the amount
  shrank between two attempts) is ``ProviderUnavailable``, and the next attempt finds the
  refund by its metadata. Then ``DELETE /v1/subscriptions/{id}`` with ``invoice_now=false`` and
  ``prorate=false`` ends a subscription at once, without a final invoice and without a credit
  for the unused time. ``DELETE /v1/customers/{id}`` deletes the customer
  object (and ends any subscription Billing never heard of). Stripe keeps the invoices and
  payments of a deleted customer for as long as it must; they are no longer reachable from
  MowDeck. Both calls are idempotent here: Stripe's ``resource_missing`` and a subscription
  that has ended already count as done. A rate limit, a timeout, a connection error and a 5xx
  (after the SDK's own retries) are ``ProviderUnavailable``, everything else ``ProviderError``.
"""

import json
from typing import Any

import stripe

from billing.adapters.providers.stripe_events import (
    event_from,
    paid_invoice_from,
    refund_from,
    subscription_from,
)
from billing.domain.errors import (
    InvalidSignature,
    ProviderError,
    ProviderUnavailable,
    RefundImpossible,
)
from billing.domain.models import (
    ENDED_STATUSES,
    BillingAccount,
    LineKind,
    PaidInvoice,
    PlanPrice,
    ProviderEvent,
    ProviderRefund,
    ProviderSubscription,
)

WEBHOOK_TOLERANCE_SECONDS = 300
"""Accepted age of a webhook signature (Stripe's default)."""

RESOURCE_MISSING = "resource_missing"
"""Stripe's error code for an object it does not know (any more): a deleted customer, a
subscription of a deleted customer."""

CANCEL_NOW = {"invoice_now": False, "prorate": False}
"""How a subscription of an erased account ends: no final invoice for what is pending, no
credit for the unused time (both are Stripe's defaults, stated so that a changed default
cannot bill or credit somebody who is deleting their account)."""


PERMANENT_REFUND_CODES = frozenset(
    {
        "charge_expired_for_refund",
        "charge_disputed",
        "refund_disputed_payment",
        "charge_already_refunded",
    }
)
"""Stripe's error codes for a refund that asking again cannot make: the payment is older than
Stripe allows, it is disputed, or nothing of it is left to refund."""

INVOICE_PAGE = 10
"""Paid invoices of a subscription looked at for the latest one that bills a period."""


def _missing(exc: stripe.StripeError) -> bool:
    return isinstance(exc, stripe.InvalidRequestError) and exc.code == RESOURCE_MISSING


def _failure(message: str, exc: stripe.StripeError) -> ProviderError:
    """``ProviderUnavailable`` for what can succeed later, ``ProviderError`` for the rest."""
    later = isinstance(exc, stripe.RateLimitError | stripe.APIConnectionError) or (
        (exc.http_status or 0) >= 500
    )
    return ProviderUnavailable(message) if later else ProviderError(message)


def verify_stripe_payload(
    payload: bytes,
    signature: str | None,
    secret: str,
    tolerance: int = WEBHOOK_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    """Verify ``Stripe-Signature`` over the raw body and return the parsed event."""
    if not secret:
        raise InvalidSignature("no webhook secret is configured")
    if not signature:
        raise InvalidSignature("missing Stripe-Signature header")
    try:
        text = payload.decode("utf-8")
        stripe.WebhookSignature.verify_header(text, signature, secret, tolerance)
        event = json.loads(text)
    except (UnicodeDecodeError, ValueError, stripe.SignatureVerificationError) as exc:
        raise InvalidSignature("invalid webhook signature or payload") from exc
    if not isinstance(event, dict) or not isinstance(event.get("id"), str):
        raise InvalidSignature("the payload is not an event")
    return event


class StripeProvider:
    """``PaymentProvider`` on Stripe."""

    name = "stripe"

    def __init__(
        self,
        secret_key: str,
        webhook_secret: str,
        *,
        api_base: str | None = None,
        automatic_tax: bool = False,
        max_network_retries: int = 2,
        client: stripe.StripeClient | None = None,
    ) -> None:
        self.webhook_secret = webhook_secret
        self.automatic_tax = automatic_tax
        if client is None:
            options: dict[str, Any] = {"max_network_retries": max_network_retries}
            if api_base:
                options["base_addresses"] = {"api": api_base.rstrip("/")}
            client = stripe.StripeClient(secret_key, **options)
        self.client = client

    async def create_customer(self, account: BillingAccount) -> str:
        params: dict[str, Any] = {"metadata": {"account_id": str(account.account_id)}}
        if account.name:
            params["name"] = account.name
        try:
            customer = await self.client.v1.customers.create_async(
                params,  # type: ignore[arg-type]
                {"idempotency_key": f"mowdeck-customer-{account.account_id}"},
            )
        except stripe.StripeError as exc:
            raise ProviderError("creating the customer at Stripe failed") from exc
        return str(customer.id)

    def checkout_params(
        self,
        account: BillingAccount,
        customer_id: str,
        plan: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        trial_days: int = 0,
    ) -> dict[str, Any]:
        """The parameters of ``POST /v1/checkout/sessions`` (separate for the tests)."""
        metadata = {"account_id": str(account.account_id), "plan": plan}
        subscription_data: dict[str, Any] = {"metadata": metadata}
        if trial_days > 0:
            subscription_data["trial_period_days"] = trial_days
        params: dict[str, Any] = {
            "mode": "subscription",
            "customer": customer_id,
            "client_reference_id": str(account.account_id),
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": metadata,
            "subscription_data": subscription_data,
        }
        business = account.collects_business_details
        if business:
            params["tax_id_collection"] = {"enabled": True}
            params["billing_address_collection"] = "required"
        if self.automatic_tax:
            params["automatic_tax"] = {"enabled": True}
            params.setdefault("billing_address_collection", "required")
        if business or self.automatic_tax:
            params["customer_update"] = {"address": "auto", "name": "auto"}
        return params

    async def create_checkout(
        self,
        account: BillingAccount,
        customer_id: str,
        plan: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        trial_days: int = 0,
    ) -> str:
        params = self.checkout_params(
            account, customer_id, plan, price_id, success_url, cancel_url, trial_days
        )
        try:
            session = await self.client.v1.checkout.sessions.create_async(params)  # type: ignore[arg-type]
        except stripe.StripeError as exc:
            raise ProviderError("creating the checkout session at Stripe failed") from exc
        if not session.url:
            raise ProviderError("Stripe returned a checkout session without URL")
        return str(session.url)

    async def create_portal(self, customer_id: str, return_url: str) -> str:
        try:
            session = await self.client.v1.billing_portal.sessions.create_async(
                {"customer": customer_id, "return_url": return_url}
            )
        except stripe.StripeError as exc:
            raise ProviderError("opening the customer portal at Stripe failed") from exc
        return str(session.url)

    def parse_webhook(self, payload: bytes, signature: str | None) -> ProviderEvent:
        return event_from(verify_stripe_payload(payload, signature, self.webhook_secret))

    async def fetch_subscription(self, subscription_id: str) -> ProviderSubscription:
        try:
            found = await self.client.v1.subscriptions.retrieve_async(subscription_id)
        except stripe.StripeError as exc:
            raise ProviderError("fetching the subscription from Stripe failed") from exc
        return subscription_from(found.to_dict())

    async def cancel_subscription(self, subscription_id: str) -> None:
        try:
            await self.client.v1.subscriptions.cancel_async(
                subscription_id,
                CANCEL_NOW,  # type: ignore[arg-type]
            )
        except stripe.StripeError as exc:
            if _missing(exc) or await self._has_ended(subscription_id, exc):
                return
            raise _failure("cancelling the subscription at Stripe failed", exc) from exc

    async def _has_ended(self, subscription_id: str, refusal: stripe.StripeError) -> bool:
        """Whether Stripe refused to cancel because the subscription is over already."""
        if not isinstance(refusal, stripe.InvalidRequestError):
            return False
        try:
            found = await self.client.v1.subscriptions.retrieve_async(subscription_id)
        except stripe.StripeError as exc:
            return _missing(exc)
        return subscription_from(found.to_dict()).status in ENDED_STATUSES

    async def delete_customer(self, customer_id: str) -> None:
        try:
            await self.client.v1.customers.delete_async(customer_id)
        except stripe.StripeError as exc:
            if _missing(exc):
                return
            raise _failure("deleting the customer at Stripe failed", exc) from exc

    async def latest_paid_invoice(self, subscription_id: str) -> PaidInvoice | None:
        try:
            page = await self.client.v1.invoices.list_async(
                {"subscription": subscription_id, "status": "paid", "limit": INVOICE_PAGE}
            )
            for invoice in page.data:
                obj = invoice.to_dict()
                lines = await self._lines(obj)
                found = paid_invoice_from(obj, subscription_id, lines, None)
                if not any(line.kind is LineKind.RECURRING for line in found.lines):
                    continue  # prorations or one-off items alone
                payment = await self._payment(str(obj["id"]))
                return paid_invoice_from(obj, subscription_id, lines, payment)
        except stripe.StripeError as exc:
            raise _failure("reading the paid invoices at Stripe failed", exc) from exc
        return None

    async def _lines(self, invoice: dict[str, Any]) -> list[dict[str, Any]]:
        """Every line of an invoice (the invoice object carries the first page only)."""
        lines = invoice.get("lines")
        lines = lines if isinstance(lines, dict) else {}
        data = lines.get("data")
        first = [line for line in data if isinstance(line, dict)] if isinstance(data, list) else []
        if not lines.get("has_more"):
            return first
        found: list[dict[str, Any]] = []
        params: dict[str, Any] = {"limit": 100}
        while True:
            page = await self.client.v1.invoices.line_items.list_async(
                str(invoice["id"]),
                params,  # type: ignore[arg-type]
            )
            found.extend(line.to_dict() for line in page.data)
            if not page.has_more or not page.data:
                return found
            params = {"limit": 100, "starting_after": page.data[-1].id}

    async def _payment(self, invoice_id: str) -> dict[str, Any] | None:
        """The payment that paid the invoice (None when it was paid without one)."""
        page = await self.client.v1.invoice_payments.list_async(
            {"invoice": invoice_id, "status": "paid", "limit": 10}
        )
        for payment in page.data:
            obj = payment.to_dict()
            inner = obj.get("payment")
            if isinstance(inner, dict) and inner.get("type") in {"payment_intent", "charge"}:
                return obj
        return None

    @staticmethod
    def _payment_param(payment_ref: str) -> dict[str, str]:
        key = "charge" if payment_ref.startswith(("ch_", "py_")) else "payment_intent"
        return {key: payment_ref}

    async def refunds_of(self, payment_ref: str) -> list[ProviderRefund]:
        params: dict[str, Any] = {**self._payment_param(payment_ref), "limit": 100}
        try:
            page = await self.client.v1.refunds.list_async(params)  # type: ignore[arg-type]
        except stripe.StripeError as exc:
            raise _failure("reading the refunds at Stripe failed", exc) from exc
        return [refund_from(refund.to_dict()) for refund in page.data]

    def refund_params(
        self, payment_ref: str, amount: int, metadata: dict[str, str]
    ) -> dict[str, Any]:
        """The parameters of ``POST /v1/refunds`` (separate for the tests). No ``currency``:
        Stripe refunds in the payment's currency (the parameter is for customer balance
        refunds only)."""
        return {
            **self._payment_param(payment_ref),
            "amount": amount,
            "reason": "requested_by_customer",
            "metadata": dict(metadata),
        }

    async def refund(
        self,
        payment_ref: str,
        amount: int,
        currency: str,
        idempotency_key: str,
        metadata: dict[str, str],
    ) -> ProviderRefund:
        try:
            made = await self.client.v1.refunds.create_async(
                self.refund_params(payment_ref, amount, metadata),  # type: ignore[arg-type]
                {"idempotency_key": idempotency_key},
            )
        except stripe.IdempotencyError as exc:
            msg = "the refund's idempotency key was used with other parameters; asked again later"
            raise ProviderUnavailable(msg) from exc
        except stripe.StripeError as exc:
            if isinstance(exc, stripe.InvalidRequestError) and exc.code in PERMANENT_REFUND_CODES:
                msg = f"Stripe refuses the refund for good ({exc.code})"
                raise RefundImpossible(msg) from exc
            raise _failure("refunding the payment at Stripe failed", exc) from exc
        del currency  # the payment's own: Stripe takes no currency for a payment's refund
        return refund_from(made.to_dict())

    async def fetch_price(self, plan_key: str, price_id: str) -> PlanPrice:
        try:
            price = (await self.client.v1.prices.retrieve_async(price_id)).to_dict()
        except stripe.StripeError as exc:
            raise ProviderError("fetching the price from Stripe failed") from exc
        recurring = price.get("recurring") if isinstance(price.get("recurring"), dict) else {}
        amount = price.get("unit_amount")
        count = recurring.get("interval_count") if recurring else None
        return PlanPrice(
            plan_key=plan_key,
            price_id=price_id,
            currency=str(price.get("currency") or "") or None,
            unit_amount=amount if isinstance(amount, int) else None,
            interval=recurring.get("interval") if recurring else None,
            interval_count=count if isinstance(count, int) else None,
        )
