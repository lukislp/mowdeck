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
"""

import json
from typing import Any

import stripe

from billing.adapters.providers.stripe_events import event_from, subscription_from
from billing.domain.errors import InvalidSignature, ProviderError
from billing.domain.models import BillingAccount, PlanPrice, ProviderEvent, ProviderSubscription

WEBHOOK_TOLERANCE_SECONDS = 300
"""Accepted age of a webhook signature (Stripe's default)."""


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

    async def cancel_subscription(self, subscription_id: str) -> ProviderSubscription:
        try:
            cancelled = await self.client.v1.subscriptions.cancel_async(subscription_id)
        except stripe.StripeError as exc:
            raise ProviderError("cancelling the subscription at Stripe failed") from exc
        return subscription_from(cancelled.to_dict())

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
