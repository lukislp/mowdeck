# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The Stripe adapter: request parameters, and calls against stripe-mock.

stripe-mock validates every request against Stripe's OpenAPI document and answers with fixtures;
it keeps no state and sends no webhooks. The integration tests run when
``MOWDECK_TEST_STRIPE_MOCK_URL`` is set (CI starts the container)::

    docker run -d --name stripe-mock -p 12111:12111 stripe/stripe-mock:v0.206.0
    MOWDECK_TEST_STRIPE_MOCK_URL=http://127.0.0.1:12111 uv run pytest services/billing -k stripe
"""

import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import stripe
from mowdeck_contracts.v1 import AccountKind, SubscriptionStatus

from billing.adapters.providers.fixtures import encode, sign, subscription_event
from billing.adapters.providers.stripe import StripeProvider
from billing.domain.errors import InvalidSignature, ProviderError, ProviderUnavailable
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
        provider.delete_customer("cus"),
        provider.fetch_price("home", "price"),
    ]
    for call in calls:
        with pytest.raises(ProviderError):
            await call


# --- account erasure (ADR 0082): the two calls and what counts as done -------------------------


class _Api:
    """The three Stripe calls of the erasure, answered or failed by the test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.cancel_error: stripe.StripeError | None = None
        self.delete_error: stripe.StripeError | None = None
        self.retrieve_error: stripe.StripeError | None = None
        self.status = "active"

    async def cancel_async(self, subscription_id: str, params: Any = None) -> Any:
        self.calls.append(("cancel", subscription_id, params))
        if self.cancel_error is not None:
            raise self.cancel_error
        return SimpleNamespace(to_dict=lambda: {"id": subscription_id, "status": "canceled"})

    async def retrieve_async(self, subscription_id: str) -> Any:
        self.calls.append(("retrieve", subscription_id, None))
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return SimpleNamespace(to_dict=lambda: {"id": subscription_id, "status": self.status})

    async def delete_async(self, customer_id: str) -> Any:
        self.calls.append(("delete", customer_id, None))
        if self.delete_error is not None:
            raise self.delete_error
        return SimpleNamespace(id=customer_id, deleted=True)


def _with(api: _Api) -> StripeProvider:
    client = SimpleNamespace(v1=SimpleNamespace(subscriptions=api, customers=api))
    return StripeProvider("sk_test_123", "whsec", client=cast(stripe.StripeClient, client))


def _error(kind: type[stripe.StripeError], *args: Any, **values: Any) -> stripe.StripeError:
    """A Stripe error as the SDK raises it (its constructors are not annotated)."""
    error: stripe.StripeError = cast(Any, kind)(*args, **values)
    return error


def _refused(message: str = "refused") -> stripe.StripeError:
    return _error(stripe.InvalidRequestError, message, None, http_status=400)


def _missing(what: str) -> stripe.StripeError:
    return _error(
        stripe.InvalidRequestError,
        f"No such {what}",
        "id",
        code="resource_missing",
        http_status=404,
    )


async def test_billing_stripe_cancels_now_without_invoice_or_proration() -> None:
    api = _Api()
    await _with(api).cancel_subscription("sub_1")
    assert api.calls == [("cancel", "sub_1", {"invoice_now": False, "prorate": False})]


async def test_billing_stripe_a_subscription_that_is_gone_or_over_counts_as_cancelled() -> None:
    gone = _Api()
    gone.cancel_error = _missing("subscription")
    await _with(gone).cancel_subscription("sub_1")
    assert [c[0] for c in gone.calls] == ["cancel"]

    over = _Api()
    over.cancel_error = _refused("already canceled")
    over.status = "canceled"
    await _with(over).cancel_subscription("sub_1")
    assert [c[0] for c in over.calls] == ["cancel", "retrieve"]

    vanished = _Api()
    vanished.cancel_error = _refused()
    vanished.retrieve_error = _missing("subscription")
    await _with(vanished).cancel_subscription("sub_1")


async def test_billing_stripe_a_refused_cancellation_of_a_running_subscription_fails() -> None:
    api = _Api()
    api.cancel_error = _refused()
    with pytest.raises(ProviderError) as refused:
        await _with(api).cancel_subscription("sub_1")
    assert not isinstance(refused.value, ProviderUnavailable)

    unclear = _Api()
    unclear.cancel_error = _refused()
    unclear.retrieve_error = _error(stripe.APIConnectionError, "timeout")
    with pytest.raises(ProviderError):
        await _with(unclear).cancel_subscription("sub_1")


async def test_billing_stripe_a_customer_that_is_gone_counts_as_deleted() -> None:
    api = _Api()
    await _with(api).delete_customer("cus_1")
    api.delete_error = _missing("customer")
    await _with(api).delete_customer("cus_1")
    assert api.calls == [("delete", "cus_1", None), ("delete", "cus_1", None)]


@pytest.mark.parametrize(
    ("error", "later"),
    [
        (_error(stripe.RateLimitError, "slow down", http_status=429), True),
        (_error(stripe.APIConnectionError, "timed out"), True),
        (_error(stripe.APIError, "stripe is down", http_status=503), True),
        (_error(stripe.AuthenticationError, "bad key", http_status=401), False),
        (_error(stripe.PermissionError, "restricted key", http_status=403), False),
    ],
)
async def test_billing_stripe_failures_say_whether_a_retry_can_help(
    error: stripe.StripeError, later: bool
) -> None:
    """Either way the erasure fails and Identity asks again; the name of the error tells the
    operator whether waiting is enough. No rate limit or timeout is ever a success."""
    api = _Api()
    api.cancel_error = api.delete_error = error
    for call in (_with(api).cancel_subscription("sub_1"), _with(api).delete_customer("cus_1")):
        with pytest.raises(ProviderError) as failed:
            await call
        assert isinstance(failed.value, ProviderUnavailable) is later
    assert [c[0] for c in api.calls] == ["cancel", "delete"]


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
    # The erasure calls (ADR 0082): stripe-mock checks the parameters of the cancellation.
    await provider.cancel_subscription("sub_123")
    await provider.delete_customer("cus_123")
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
