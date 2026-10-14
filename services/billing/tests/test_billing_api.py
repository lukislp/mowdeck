"""The HTTP API: health, descriptor, status, authorization, checkout, portal, subscription."""

from billing_support import API, harness
from mowdeck_authz import RelationshipTuple, account, user
from mowdeck_contracts.v1 import AccountCreated, AccountKind
from test_billing_webhooks import _sub

from billing.adapters.providers.fake import FakeProvider
from billing.domain.models import OfferedPlan
from billing.domain.prices import PriceTable


async def test_billing_health_and_descriptor() -> None:
    async with harness() as h:
        assert (await h.client.get("/healthz")).status_code == 200
        assert (await h.client.get("/readyz")).status_code == 200
        descriptor = (await h.client.get("/.well-known/mowdeck-module")).json()
        assert descriptor["name"] == "billing"
        assert descriptor["permissions"] == []
        assert descriptor["events"][0]["type"] == "billing.subscription.changed"


async def test_billing_status_is_public_with_prices_and_names() -> None:
    async with harness(provider=FakeProvider(webhook_secret="x", amounts={"home": 499})) as h:  # noqa: S106 - test value
        response = await h.client.get(f"{API}/status")
        assert response.status_code == 200
        body = response.json()
        assert (body["enabled"], body["provider"], body["currency"], body["automatic_tax"]) == (
            True,
            "fake",
            "eur",
            False,
        )
        plans = {p["key"]: p for p in body["plans"]}
        assert set(plans) == {"home", "fleet"}  # "x" has no price id
        assert plans["home"]["name"] == "Home"
        assert plans["home"]["trial_days"] == 30
        assert plans["home"]["price"] == {
            "price_id": "price_home",
            "currency": "eur",
            "unit_amount": 499,
            "interval": "month",
            "interval_count": 1,
        }


async def test_billing_status_degrades_without_catalogue_and_prices() -> None:
    fake = FakeProvider(webhook_secret="x", fail_prices=True)  # noqa: S106 - test value
    async with harness(provider=fake, plans=[]) as h:
        body = (await h.client.get(f"{API}/status")).json()
        assert {p["key"] for p in body["plans"]} == {"home", "fleet"}
        assert all(p["name"] is None and p["price"]["unit_amount"] is None for p in body["plans"])
    # A priced plan Entitlements no longer offers is left out.
    async with harness(plans=[OfferedPlan(key="home", name="Home")]) as h:
        body = (await h.client.get(f"{API}/status")).json()
        assert [p["key"] for p in body["plans"]] == ["home"]


async def test_billing_prices_are_cached() -> None:
    fake = FakeProvider(webhook_secret="x", amounts={"home": 100})  # noqa: S106 - test value
    async with harness(provider=fake) as h:
        await h.client.get(f"{API}/status")
        fake.amounts["home"] = 200
        cached = (await h.client.get(f"{API}/status")).json()
        assert {p["key"]: p["price"]["unit_amount"] for p in cached["plans"]}["home"] == 100
        h.clock.advance(seconds=601)
        fresh = (await h.client.get(f"{API}/status")).json()
        assert {p["key"]: p["price"]["unit_amount"] for p in fresh["plans"]}["home"] == 200


async def test_billing_account_endpoints_owner_admin_foreign_and_anonymous() -> None:
    async with harness() as h:
        owner = await h.owner()
        admin = await h.member("admin", owner.account_id)
        stranger = h.person(owner.account_id)  # a token for the account, but no membership
        for method, path, body in [
            ("GET", "/subscription", None),
            ("GET", "/invoices", None),
            ("POST", "/checkout", {"plan": "home"}),
            ("POST", "/portal", None),
        ]:
            anonymous = await h.client.request(method, f"{API}{path}", json=body)
            assert anonymous.status_code == 401, path
            as_admin = await h.client.request(
                method, f"{API}{path}", json=body, headers=admin.headers
            )
            assert as_admin.status_code == 403, path
            foreign = await h.client.request(
                method, f"{API}{path}", json=body, headers=stranger.headers
            )
            assert foreign.status_code == 404, path
        summary = await h.client.get(f"{API}/subscription", headers=owner.headers)
        assert summary.status_code == 200
        assert summary.json() == {"provider": "fake", "has_customer": False, "subscription": None}


async def test_billing_member_with_billing_grant_may_manage() -> None:
    async with harness() as h:
        owner = await h.owner()
        member = await h.member("member", owner.account_id)
        denied = await h.client.get(f"{API}/subscription", headers=member.headers)
        assert denied.status_code == 404  # members without can_view see nothing either
        grant = RelationshipTuple(
            user(member.principal.subject), "grant_billing_manage", account(owner.account_id)
        )
        await h.identity.write([grant])
        allowed = await h.client.get(f"{API}/subscription", headers=member.headers)
        assert allowed.status_code == 200


async def test_billing_checkout_creates_customer_once_with_trial_and_urls() -> None:
    async with harness() as h:
        owner = await h.owner()
        response = await h.client.post(
            f"{API}/checkout",
            json={"plan": "home"},
            headers={**owner.headers, "X-Forwarded-Proto": "https", "Host": "mow.example.org"},
        )
        assert response.status_code == 200, response.text
        url = response.json()["url"]
        assert url.startswith("https://mow.example.org/account?billing=fake-checkout")
        checkout = h.provider.checkouts[-1]
        assert checkout["account_id"] == owner.account_id
        assert (checkout["plan"], checkout["price"], checkout["trial_days"]) == (
            "home",
            "price_home",
            30,
        )
        assert checkout["success_url"] == "https://mow.example.org/account?billing=success#plan"
        assert checkout["cancel_url"] == "https://mow.example.org/account?billing=cancel#plan"
        assert checkout["business"] is True  # kind unknown: business fields offered
        # A second checkout reuses the customer.
        await h.client.post(f"{API}/checkout", json={"plan": "fleet"}, headers=owner.headers)
        assert len(h.provider.customers) == 1
        assert h.provider.checkouts[-1]["customer"] == checkout["customer"]


async def test_billing_checkout_household_without_trial_after_a_subscription() -> None:
    async with harness(billing_app_url="https://app.example.org/") as h:
        owner = await h.owner()
        await h.service.account_created(
            AccountCreated(
                account_id=owner.account_id,
                name="Garden",
                kind=AccountKind.HOUSEHOLD,
                owner_user_id=owner.principal.subject,
            )
        )
        await h.client.post(f"{API}/checkout", json={"plan": "home"}, headers=owner.headers)
        first = h.provider.checkouts[-1]
        assert first["business"] is False
        assert first["success_url"].startswith("https://app.example.org/account")
        # The subscription ended: a new checkout gets no second trial.
        await h.webhook(_sub(h, owner.account_id, "canceled", ended_at=h.clock.ts - 400 * 86400))
        await h.client.post(f"{API}/checkout", json={"plan": "home"}, headers=owner.headers)
        assert h.provider.checkouts[-1]["trial_days"] == 0


async def test_billing_checkout_refusals() -> None:
    async with harness() as h:
        owner = await h.owner()
        not_sold = await h.client.post(f"{API}/checkout", json={"plan": "x"}, headers=owner.headers)
        assert not_sold.status_code == 422
        invalid = await h.client.post(
            f"{API}/checkout", json={"plan": "Bad Key"}, headers=owner.headers
        )
        assert invalid.status_code == 422
        await h.webhook(_sub(h, owner.account_id, "active"))
        conflict = await h.client.post(
            f"{API}/checkout", json={"plan": "home"}, headers=owner.headers
        )
        assert conflict.status_code == 409
        assert conflict.json()["type"] == "urn:mowdeck:problem:conflict"


async def test_billing_portal_needs_a_customer() -> None:
    async with harness() as h:
        owner = await h.owner()
        missing = await h.client.post(f"{API}/portal", headers=owner.headers)
        assert missing.status_code == 404
        await h.client.post(f"{API}/checkout", json={"plan": "home"}, headers=owner.headers)
        portal = await h.client.post(f"{API}/portal", headers=owner.headers)
        assert portal.status_code == 200
        assert "billing=fake-portal" in portal.json()["url"]
        summary = (await h.client.get(f"{API}/subscription", headers=owner.headers)).json()
        assert summary["has_customer"] is True


async def test_billing_subscription_summary_after_webhook() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.webhook(_sub(h, owner.account_id, "past_due", cancel_at_period_end=True))
        body = (await h.client.get(f"{API}/subscription", headers=owner.headers)).json()
        subscription = body["subscription"]
        assert (subscription["plan"], subscription["status"], subscription["plan_status"]) == (
            "home",
            "past_due",
            "grace",
        )
        assert subscription["cancel_at_period_end"] is True
        assert subscription["grace_until"] is not None


async def test_billing_provider_errors_are_503() -> None:
    class BrokenProvider(FakeProvider):
        async def create_customer(self, account: object) -> str:
            from billing.domain.errors import ProviderError

            raise ProviderError("Stripe is down")

    async with harness(provider=BrokenProvider(webhook_secret="x")) as h:  # noqa: S106 - test value
        owner = await h.owner()
        response = await h.client.post(
            f"{API}/checkout", json={"plan": "home"}, headers=owner.headers
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "Stripe is down"


async def test_billing_prices_overrides_from_settings() -> None:
    async with harness(prices=PriceTable().with_overrides("home=price_env")) as h:
        body = (await h.client.get(f"{API}/status")).json()
        assert [(p["key"], p["price"]["price_id"]) for p in body["plans"]] == [
            ("home", "price_env")
        ]
