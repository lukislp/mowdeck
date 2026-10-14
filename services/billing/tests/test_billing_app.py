"""Wiring: settings, provider and price loading, the bus adapters, the plan catalogue, the CLI."""

import argparse
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from billing_support import SECRET, harness, make_settings
from mowdeck_auth.testing import TestKeys
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    IDENTITY_ACCOUNT_CREATED,
    AccountCreated,
    AccountKind,
    EventEnvelope,
    PlanSetRequest,
    PlanStatus,
    SubscriptionChanged,
    SubscriptionStatus,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit.bus import InMemoryBus, publish_event
from mowdeck_service_kit.testing import lifespan_client

from billing import cli
from billing.adapters.bus.entitlements import BusEntitlementsGateway
from billing.adapters.bus.publisher import BusEventPublisher
from billing.adapters.memory import MemoryStore
from billing.adapters.plans import HttpPlanCatalog, StaticPlanCatalog
from billing.adapters.providers.fake import FakeProvider
from billing.adapters.providers.stripe import StripeProvider, verify_stripe_payload
from billing.domain.errors import InvalidInput, NotFoundError, ProviderError
from billing.main import build_app, build_provider, default_principal_resolver, load_prices

REPO = Path(__file__).resolve().parents[3]


def test_billing_settings_defaults() -> None:
    settings = make_settings()
    assert settings.service_name == "billing"
    assert settings.billing_grace_days == 14
    assert settings.billing_prices_file == "/etc/mowdeck/billing/prices.yaml"
    assert settings.billing_entitlements_url == ""
    assert make_settings(billing_entitlements_url="http://e:8000").billing_entitlements_url


def test_billing_build_provider_choices() -> None:
    fake = build_provider(make_settings())
    assert isinstance(fake, FakeProvider)
    assert fake.webhook_secret == SECRET
    with pytest.raises(ValueError, match="SECRET_KEY"):
        build_provider(make_settings(billing_provider="stripe"))
    stripe_provider = build_provider(
        make_settings(
            billing_provider="stripe",
            billing_stripe_secret_key="sk_test_1",  # noqa: S106 - test value
            billing_stripe_webhook_secret=None,
            billing_stripe_api_base="http://stripe-mock:12111",
            billing_automatic_tax=True,
        )
    )
    assert isinstance(stripe_provider, StripeProvider)
    assert stripe_provider.automatic_tax
    assert stripe_provider.webhook_secret == ""


def test_billing_load_prices_from_file_and_overrides(tmp_path: Path) -> None:
    repository = load_prices(
        make_settings(billing_prices_file=str(REPO / "infra" / "billing" / "prices.yaml"))
    )
    assert repository.currency == "eur"
    assert set(repository.prices) == {"home", "fleet"}
    assert repository.sold == {}  # the repository sells nothing until the operator sets ids
    file = tmp_path / "prices.yaml"
    file.write_text("currency: chf\nprices:\n  home: price_file\n", encoding="utf-8")
    table = load_prices(
        make_settings(billing_prices_file=str(file), billing_price_ids="fleet=price_env")
    )
    assert (table.currency, table.sold) == ("chf", {"home": "price_file", "fleet": "price_env"})
    missing = load_prices(make_settings(billing_prices_file=str(tmp_path / "none.yaml")))
    assert missing.sold == {}
    file.write_text("prices: [", encoding="utf-8")
    with pytest.raises(InvalidInput):
        load_prices(make_settings(billing_prices_file=str(file)))


def test_billing_repository_plans_have_price_entries() -> None:
    """Every plan of the price file exists in the Entitlements plan file."""
    import yaml

    plans = yaml.safe_load((REPO / "infra" / "entitlements" / "plans.yaml").read_text("utf-8"))
    keys = {plan["key"] for plan in plans["plans"]}
    table = load_prices(
        make_settings(billing_prices_file=str(REPO / "infra" / "billing" / "prices.yaml"))
    )
    assert set(table.prices) <= keys


def test_billing_build_app_needs_a_database() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        build_app(make_settings(), provider=FakeProvider(), authorizer=InMemoryAuthorizer())


def test_billing_build_app_with_database_url_and_http_catalog() -> None:
    app = build_app(
        make_settings(
            database_url="postgresql://u:p@127.0.0.1:1/db",
            billing_entitlements_url="http://entitlements:8000",
        ),
        provider=FakeProvider(),
        authorizer=InMemoryAuthorizer(),
    )
    assert app.title


def test_billing_default_principal_resolver() -> None:
    assert default_principal_resolver({}) is None
    assert default_principal_resolver({"MOWDECK_AUTH_JWKS_URL": "http://i/jwks"}) is not None


async def test_billing_remembers_created_accounts_from_the_bus() -> None:
    bus = InMemoryBus()
    store = MemoryStore()
    keys = TestKeys()
    app = build_app(
        make_settings(),
        bus=bus,
        store=store,
        provider=FakeProvider(webhook_secret=SECRET),
        authorizer=InMemoryAuthorizer(),
        principal_resolver=keys.resolver(),
        catalog=StaticPlanCatalog(),
    )
    async with lifespan_client(app):
        account_id = uuid4()
        envelope = EventEnvelope[AccountCreated](
            type=IDENTITY_ACCOUNT_CREATED,
            producer="identity",
            account_id=account_id,
            payload=AccountCreated(
                account_id=account_id,
                name="Firma",
                kind=AccountKind.BUSINESS,
                owner_user_id=uuid4(),
            ),
        )
        await publish_event(bus, envelope)
        await publish_event(bus, envelope)  # idempotent
        await bus.publish(event_subject_for_type(IDENTITY_ACCOUNT_CREATED), b"{}")  # dropped
        async with store.transaction() as tx:
            account = await tx.get_account(account_id)
        assert account is not None
        assert (account.name, account.kind) == ("Firma", AccountKind.BUSINESS)
        assert len(app.state.subscriptions) == 2  # account.created, grants.changed


async def test_billing_gateway_and_publisher_without_bus() -> None:
    gateway = BusEntitlementsGateway(None)
    with pytest.raises(ProviderError):
        await gateway.set_plan(
            PlanSetRequest(account_id=uuid4(), plan="home", status=PlanStatus.ACTIVE, reason="x")
        )
    # Without a bus the event is only logged; a failing bus is swallowed too.
    change = SubscriptionChanged(
        account_id=uuid4(), provider="fake", status=SubscriptionStatus.ACTIVE
    )
    await BusEventPublisher(None).subscription_changed(change)

    class BrokenBus(InMemoryBus):
        async def publish(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("down")

    await BusEventPublisher(BrokenBus()).subscription_changed(change)


async def test_billing_http_plan_catalog() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/entitlements/plans"
        return httpx.Response(
            200,
            json=[
                {"key": "home", "name": "Home", "trial_days": 30, "features": ["view"]},
                {"key": "old", "name": "Old", "active": False},
                "junk",
            ],
        )

    catalog = HttpPlanCatalog("http://e:8000/", transport=httpx.MockTransport(answer))
    plans = await catalog.offered()
    assert [(p.key, p.name, p.trial_days, p.features) for p in plans] == [
        ("home", "Home", 30, ["view"])
    ]
    broken = HttpPlanCatalog(
        "http://e", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(ValueError, match="unexpected"):
        await broken.offered()
    down = HttpPlanCatalog("http://e", transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(httpx.HTTPStatusError):
        await down.offered()


async def test_billing_fake_provider_subscription_lookup() -> None:
    fake = FakeProvider()
    with pytest.raises(NotFoundError):
        await fake.fetch_subscription("sub_missing")


def test_billing_cli_send_fixture_signs_the_event() -> None:
    seen: dict[str, Any] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["signature"] = request.headers["Stripe-Signature"]
        return httpx.Response(200, json={"received": True})

    account_id = uuid4()
    client = httpx.Client(transport=httpx.MockTransport(answer))
    args = argparse.Namespace(
        url="http://billing/api/v1/billing/webhooks/stripe",
        account_id=account_id,
        status="past_due",
        plan="home",
        price_id="price_fixture_home",
        subscription_id=None,
        customer_id=None,
        event_type="customer.subscription.updated",
        secret="whsec_cli",  # noqa: S106 - test value
    )
    assert cli.send_fixture(args, client) == 0
    event = verify_stripe_payload(seen["body"], seen["signature"], "whsec_cli")
    assert event["data"]["object"]["status"] == "past_due"
    assert event["data"]["object"]["metadata"]["account_id"] == str(account_id)
    refused = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400)))
    assert cli.send_fixture(args, refused) == 1
    args.secret = None
    assert cli.send_fixture(args, client) == 2


def test_billing_cli_parser() -> None:
    with pytest.raises(SystemExit):
        cli.main(["send-fixture", "--status", "nope", "--account-id", str(uuid4())])


async def test_billing_harness_status_reachable() -> None:
    async with harness() as h:
        assert (await h.client.get("/api/v1/billing/status")).status_code == 200
