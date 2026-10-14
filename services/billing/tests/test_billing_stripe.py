"""The Stripe adapter: request parameters, and calls against stripe-mock.

stripe-mock validates every request against Stripe's OpenAPI document and answers with fixtures;
it keeps no state and sends no webhooks. The integration tests run when
``MOWDECK_TEST_STRIPE_MOCK_URL`` is set (CI starts the container)::

    docker run -d --name stripe-mock -p 12111:12111 stripe/stripe-mock:v0.206.0
    MOWDECK_TEST_STRIPE_MOCK_URL=http://127.0.0.1:12111 uv run pytest services/billing -k stripe
"""

import os
from typing import Any
from uuid import uuid4

import pytest
import stripe
from mowdeck_contracts.v1 import AccountKind, SubscriptionStatus

from billing.adapters.providers.fixtures import encode, sign, subscription_event
from billing.adapters.providers.stripe import StripeProvider
from billing.domain.errors import InvalidSignature, ProviderError
from billing.domain.models import BillingAccount

MOCK_URL = os.environ.get("MOWDECK_TEST_STRIPE_MOCK_URL")
needs_mock = pytest.mark.skipif(not MOCK_URL, reason="needs MOWDECK_TEST_STRIPE_MOCK_URL")


def _provider(**kwargs: Any) -> StripeProvider:
    return StripeProvider(
        "sk_test_123", "whsec_test", api_base=MOCK_URL, max_network_retries=0, **kwargs
    )


def test_billing_stripe_checkout_params_household() -> None:
    provider = _provider()
    account = BillingAccount(uuid4(), name="Garden", kind=AccountKind.HOUSEHOLD)
    params = provider.checkout_params(
        account, "cus_1", "home", "price_1", "https://a/s", "https://a/c", trial_days=30
    )
    assert params["mode"] == "subscription"
    assert params["customer"] == "cus_1"
    assert params["client_reference_id"] == str(account.account_id)
    assert params["line_items"] == [{"price": "price_1", "quantity": 1}]
    assert params["subscription_data"] == {
        "metadata": {"account_id": str(account.account_id), "plan": "home"},
        "trial_period_days": 30,
    }
    for key in ("tax_id_collection", "automatic_tax", "customer_update", "payment_method_types"):
        assert key not in params
    assert "billing_address_collection" not in params


def test_billing_stripe_checkout_params_business_with_tax() -> None:
    provider = _provider(automatic_tax=True)
    account = BillingAccount(uuid4(), kind=AccountKind.BUSINESS)
    params = provider.checkout_params(account, "cus_1", "fleet", "price_2", "s", "c")
    assert params["tax_id_collection"] == {"enabled": True}
    assert params["billing_address_collection"] == "required"
    assert params["automatic_tax"] == {"enabled": True}
    assert params["customer_update"] == {"address": "auto", "name": "auto"}
    assert "trial_period_days" not in params["subscription_data"]
    household = provider.checkout_params(
        BillingAccount(uuid4(), kind=AccountKind.HOUSEHOLD), "cus_1", "home", "p", "s", "c"
    )
    assert household["automatic_tax"] == {"enabled": True}
    assert household["billing_address_collection"] == "required"
    assert "tax_id_collection" not in household


def test_billing_stripe_parse_webhook() -> None:
    provider = _provider()
    event = subscription_event(
        "customer.subscription.updated",
        subscription_id="sub_1",
        customer_id="cus_1",
        status="active",
        price_id="price_1",
        period_start=1,
        period_end=2,
    )
    body = encode(event)
    parsed = provider.parse_webhook(body, sign(body, "whsec_test"))
    assert parsed.subscription is not None
    assert parsed.subscription.status is SubscriptionStatus.ACTIVE
    with pytest.raises(InvalidSignature):
        provider.parse_webhook(body, sign(body, "whsec_wrong"))


async def test_billing_stripe_errors_become_provider_errors() -> None:
    # A closed port: every call fails fast with a connection error.
    provider = StripeProvider(
        "sk_test_123", "whsec", api_base="http://127.0.0.1:9", max_network_retries=0
    )
    account = BillingAccount(uuid4(), name="x")
    calls = [
        provider.create_customer(account),
        provider.create_checkout(account, "cus", "home", "price", "s", "c"),
        provider.create_portal("cus", "r"),
        provider.fetch_subscription("sub"),
        provider.cancel_subscription("sub"),
        provider.fetch_price("home", "price"),
    ]
    for call in calls:
        with pytest.raises(ProviderError):
            await call


def test_billing_stripe_client_uses_the_api_base() -> None:
    provider = StripeProvider("sk_test_123", "whsec", api_base="http://stripe-mock:12111/")
    assert isinstance(provider.client, stripe.StripeClient)
    injected = stripe.StripeClient("sk_test_456")
    assert StripeProvider("sk", "w", client=injected).client is injected


@needs_mock
async def test_billing_stripe_mock_customer_checkout_and_portal() -> None:
    provider = _provider(automatic_tax=True)
    account = BillingAccount(uuid4(), name="Firma GmbH", kind=AccountKind.BUSINESS)
    customer_id = await provider.create_customer(account)
    assert customer_id.startswith("cus_")
    url = await provider.create_checkout(
        account,
        customer_id,
        "fleet",
        "price_1",
        "http://localhost/account?billing=success",
        "http://localhost/account?billing=cancel",
        trial_days=14,
    )
    assert url.startswith("https://checkout.stripe.com/")
    portal = await provider.create_portal(customer_id, "http://localhost/account")
    assert portal.startswith("https://")


@needs_mock
async def test_billing_stripe_mock_subscription_and_price() -> None:
    provider = _provider()
    subscription = await provider.fetch_subscription("sub_123")
    assert subscription.id.startswith("sub_")
    assert subscription.status in set(SubscriptionStatus)
    assert subscription.current_period_end is not None  # from the subscription items
    cancelled = await provider.cancel_subscription("sub_123")
    assert cancelled.id.startswith("sub_")
    price = await provider.fetch_price("home", "price_123")
    assert price.plan_key == "home"
    assert price.unit_amount is not None
    assert price.interval in {"day", "week", "month", "year"}


@needs_mock
async def test_billing_stripe_mock_through_the_api() -> None:
    from billing_support import API, harness

    async with harness(provider=_provider()) as h:  # type: ignore[arg-type]
        owner = await h.owner()
        checkout = await h.client.post(
            f"{API}/checkout", json={"plan": "home"}, headers=owner.headers
        )
        assert checkout.status_code == 200, checkout.text
        assert checkout.json()["url"].startswith("https://checkout.stripe.com/")
        portal = await h.client.post(f"{API}/portal", headers=owner.headers)
        assert portal.status_code == 200, portal.text
        status = (await h.client.get(f"{API}/status")).json()
        assert status["provider"] == "stripe"
        assert all(p["price"]["unit_amount"] is not None for p in status["plans"])
