"""The bus side: ``entitlements.check`` round trip with the kit client, account events, the
feature catalogue from module descriptors, the plan seeding at startup."""

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from entitlements_support import API, PLANS_FILE, harness, repository_plans
from mowdeck_contracts.v1 import (
    ENTITLEMENTS_PLAN_CHANGED,
    MODULE_DESCRIPTOR_PATH,
    DenyReason,
    FeatureDef,
    ModuleDescriptor,
)
from mowdeck_contracts.v1.subjects import ENTITLEMENTS_CHECK, event_subject_for_type
from mowdeck_service_kit.entitlements import EntitlementsClient

from entitlements.adapters.memory import MemoryStore
from entitlements.adapters.modules import HttpModuleSource
from entitlements.application.ports import FetchedDescriptor
from entitlements.domain.errors import InvalidInput
from entitlements.main import load_plan_specs
from entitlements.settings import Settings

REALTIME = ModuleDescriptor(
    name="realtime",
    version="0.1.0",
    features=[FeatureDef(key="manual_drive", description="Manual driving")],
)


class FakeModules:
    def __init__(self, *answers: FetchedDescriptor) -> None:
        self.answers = list(answers)

    async def fetch(self) -> list[FetchedDescriptor]:
        return list(self.answers)


async def test_entitlements_check_round_trip_through_the_kit_client() -> None:
    async with harness(entitlements_billing_enabled=True) as h:
        owner = await h.owner()
        client = EntitlementsClient(h.bus, billing_enabled=True)
        reply = await client.check_limit(owner.account_id, "max_mowers", 4)
        assert (reply.allowed, reply.plan, reply.limit, reply.current) == (False, "home", 4, 4)
        assert reply.reason is DenyReason.LIMIT_REACHED
        assert (await client.check_limit(owner.account_id, "max_mowers", 3)).allowed
        assert (await client.check_feature(owner.account_id, "manual_drive")).allowed
        assert not (await client.check_feature(owner.account_id, "fleet.bulk_commands")).allowed
        assert await client.plan_of(owner.account_id) == "home"
        assert len(h.bus.messages(ENTITLEMENTS_CHECK)) == 1  # the snapshot is cached
        await client.subscribe()
        operator = await h.operator()
        assert (await h.assign(operator, owner.account_id, plan="fleet")).status_code == 200
        assert len(client) == 0  # entitlements.plan.changed dropped the snapshot
        assert (await client.check_limit(owner.account_id, "max_mowers", 4)).allowed
        assert (await client.check_feature(owner.account_id, "fleet.bulk_commands")).allowed


async def test_entitlements_check_says_unlimited_without_billing() -> None:
    async with harness() as h:
        client = EntitlementsClient(h.bus, billing_enabled=True)
        reply = await client.check_limit(uuid4(), "max_mowers", 10_000)
        assert (reply.allowed, reply.plan) == (True, "unlimited")
        assert reply.snapshot is not None and reply.snapshot.billing_enabled is False


async def test_entitlements_malformed_requests_are_dropped() -> None:
    async with harness() as h:
        with pytest.raises(Exception, match="timed out"):
            await h.bus.request(ENTITLEMENTS_CHECK, b"{}", timeout=0.2)
        await h.bus.publish(event_subject_for_type("identity.account.created"), b"{}")
        assert await h.services.entitlements.accounts(0, 10) is not None


async def test_entitlements_account_created_is_idempotent() -> None:
    async with harness(entitlements_billing_enabled=True) as h:
        account_id = uuid4()
        await h.account_created(account_id, name="First")
        await h.account_created(account_id, name="Second")
        page = await h.services.entitlements.accounts(0, 10)
        assert [v.row.account_name for v in page.items] == ["First"]
        detail = await h.services.entitlements.account(account_id)
        assert len(detail.history) == 1


async def test_entitlements_startup_announces_and_seeds_once() -> None:
    store = MemoryStore()
    async with harness(store=store) as h:
        changed = h.bus.messages(event_subject_for_type(ENTITLEMENTS_PLAN_CHANGED))
        assert len(changed) == 1  # announce_restart: every client drops its snapshots
        assert len(h.app.state.subscriptions) == 4  # check, plan.set, account.created, grants
        operator = await h.operator()
        edited = await h.client.put(
            f"{API}/plans/home",
            headers=operator.headers,
            json={"name": "Home edited", "limits": {"max_mowers": 6}, "features": ["*"]},
        )
        assert edited.status_code == 200
    # Restart with the same file: the edit survives (the file entry did not change).
    async with harness(store=store) as h:
        assert (await h.services.entitlements.plan("home")).name == "Home edited"
        assert await h.services.entitlements.seed(repository_plans()) == []
    # A changed file entry wins again.
    specs = [
        s.model_copy(update={"name": "Home 2"}) if s.key == "home" else s
        for s in repository_plans()
    ]
    async with harness(store=store, plan_specs=specs) as h:
        home = await h.services.entitlements.plan("home")
        assert (home.name, home.limits.max_mowers) == ("Home 2", 4)
    # Without a plan file the built-in unlimited plan still exists.
    async with harness(plan_specs=[]) as h:
        assert [p.key for p in await h.services.entitlements.plans()] == ["unlimited"]


async def test_entitlements_catalogue_collects_module_features() -> None:
    modules = FakeModules(
        FetchedDescriptor("http://realtime:8000", REALTIME),
        FetchedDescriptor("http://reports:8000", None),
    )
    async with harness(module_source=modules) as h:
        missing = await h.services.catalogue.refresh(fetch_modules=True)
        assert missing == ["http://reports:8000"]
        owner = await h.owner()
        body = (await h.client.get(f"{API}/features", headers=owner.headers)).json()
        assert {f["key"]: f["module"] for f in body}["manual_drive"] == "realtime"
        unknown = await h.services.entitlements.warn_unknown_features()
        assert "manual_drive" not in unknown["home"]
        assert "reports" in unknown["fleet"]


async def test_entitlements_http_module_source() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "realtime" and request.url.path == MODULE_DESCRIPTOR_PATH:
            return httpx.Response(200, json=REALTIME.model_dump(mode="json"))
        if request.url.host == "broken":
            return httpx.Response(200, json={"name": "Not Valid"})
        return httpx.Response(503)

    source = HttpModuleSource(
        ["http://realtime:8000/", "http://broken:8000", "http://down:8000", " "],
        attempts=2,
        delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    answers = {a.url: a.descriptor for a in await source.fetch()}
    assert answers["http://realtime:8000"] == REALTIME
    assert answers["http://broken:8000"] is None
    assert answers["http://down:8000"] is None
    assert len(answers) == 3


def test_entitlements_load_plan_specs(tmp_path: Path) -> None:
    assert load_plan_specs(str(tmp_path / "missing.yaml")) == []
    assert load_plan_specs("") == []
    assert [s.key for s in load_plan_specs(str(PLANS_FILE))] == ["unlimited", "home", "fleet"]
    broken = tmp_path / "plans.yaml"
    broken.write_text("plans:\n  - {key: Bad}\n", encoding="utf-8")
    with pytest.raises(InvalidInput):
        load_plan_specs(str(broken))


def test_entitlements_settings_defaults() -> None:
    settings = Settings.model_validate(
        {"entitlements_module_urls": "http://fleet:8000, ,http://realtime:8000"}
    )
    assert settings.service_name == "entitlements"
    assert settings.entitlements_billing_enabled is False
    assert settings.entitlements_default_plan == "home"
    assert settings.module_urls == ["http://fleet:8000", "http://realtime:8000"]
    config = settings.to_config()
    assert (config.billing_enabled, config.default_plan, config.grace_days) == (False, "home", 14)


def test_entitlements_build_app_needs_a_database() -> None:
    from entitlements.main import build_app

    with pytest.raises(ValueError, match="MOWDECK_DATABASE_URL"):
        build_app(Settings.model_validate({"environment": "test"}))


def test_entitlements_migrate_retries_refused_connections() -> None:
    from entitlements.adapters.db.migrate import upgrade_with_retry

    calls: list[int] = []
    sleeps: list[float] = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionRefusedError("not yet")

    upgrade_with_retry(flaky, attempts=5, delay_seconds=0.5, sleep=sleeps.append)
    assert (len(calls), sleeps) == (3, [0.5, 0.5])

    def down() -> None:
        raise ConnectionRefusedError("never")

    with pytest.raises(ConnectionRefusedError):
        upgrade_with_retry(down, attempts=2, delay_seconds=0, sleep=sleeps.append)


async def test_entitlements_background_catalogue_and_resolver() -> None:
    import asyncio

    from entitlements_support import make_settings
    from mowdeck_authz.testing import InMemoryAuthorizer
    from mowdeck_service_kit.bus import InMemoryBus
    from mowdeck_service_kit.testing import lifespan_client

    from entitlements.main import build_app, default_principal_resolver

    assert default_principal_resolver({}) is None
    app = build_app(
        make_settings(),
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=InMemoryAuthorizer(),
        module_source=FakeModules(FetchedDescriptor("http://realtime:8000", REALTIME)),
        plan_specs=repository_plans(),
        background_tasks=True,
    )
    async with lifespan_client(app):
        services = app.state.entitlements_services
        for _ in range(50):
            if "manual_drive" in await services.catalogue.known_keys():
                break
            await asyncio.sleep(0.02)
        assert "manual_drive" in await services.catalogue.known_keys()
