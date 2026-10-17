"""Wiring: settings, defaults of ``build_app``, the background job, lists for partial rights."""

import asyncio
from uuid import uuid4

import pytest
from fleet_support import API, harness, make_settings
from mowdeck_authz import OpenFgaAuthorizer, group
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from fleet.adapters.memory import MemoryStore
from fleet.main import build_app, default_principal_resolver, every, start_background_tasks


def test_fleet_settings_defaults_and_config() -> None:
    settings = make_settings(
        allowed_connection_kinds=" simulated, cloud ,,", command_ttl_seconds=45
    )
    config = settings.to_config()
    assert settings.service_name == "fleet" and settings.http_port == 8000
    assert config.allowed_connection_kinds == {"simulated", "cloud"}
    assert config.limits.default_ttl.total_seconds() == 45
    assert config.command_retention.days == 90
    assert config.default_timezone == "Europe/Berlin"


def test_fleet_build_app_needs_a_database() -> None:
    with pytest.raises(ValueError, match="MOWDECK_DATABASE_URL"):
        build_app(make_settings(), bus=InMemoryBus())


def test_fleet_principal_resolver_from_the_environment() -> None:
    assert default_principal_resolver({}) is None
    url = {"MOWDECK_AUTH_JWKS_URL": "http://identity:8000/.well-known/jwks.json"}
    assert default_principal_resolver(url) is not None


async def test_fleet_build_app_defaults_to_openfga_and_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOWDECK_OPENFGA_API_URL", "http://127.0.0.1:1")
    app = build_app(
        make_settings(database_url="postgresql://u:p@127.0.0.1:1/fleet"), bus=InMemoryBus()
    )
    assert isinstance(app.state.authorizer, OpenFgaAuthorizer)
    in_memory = build_app(make_settings(), bus=InMemoryBus(), store=MemoryStore())
    async with lifespan_client(in_memory) as client:
        assert (await client.get("/healthz")).status_code == 200
        # list, credentials, issue, resolve, drive start, drive stop, results, accounts,
        # camera relay events, grants, plans
        assert len(in_memory.state.subscriptions) == 11


async def test_fleet_every_survives_failures_and_the_retention_task_starts() -> None:
    runs: list[int] = []
    third = asyncio.Event()

    async def job() -> None:
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("first run fails")
        if len(runs) == 3:
            third.set()

    task = asyncio.create_task(every(0.01, "test", job))
    await asyncio.wait_for(third.wait(), timeout=5)
    task.cancel()
    async with harness() as h:
        (retention,) = start_background_tasks(make_settings(), h.app.state.fleet)
        assert retention.get_name() == "command-retention"
        retention.cancel()


async def test_fleet_lists_follow_partial_rights() -> None:
    async with harness() as h:
        owner = await h.owner()
        home = await h.create(owner, "sites", name="Home")
        north = await h.create(owner, "groups", name="North", site_id=home["id"])
        await h.create(owner, "groups", name="South", site_id=home["id"])
        m1 = (await h.register(owner, name="a", site_id=home["id"], group_ids=[north["id"]]))["id"]
        m2 = (await h.register(owner, name="b", site_id=home["id"]))["id"]
        anna = await h.member(owner, "viewer", group(north["id"]))
        mowers = (await h.client.get(f"{API}/mowers", headers=anna.headers)).json()["items"]
        by_group = (
            await h.client.get(
                f"{API}/mowers", headers=owner.headers, params={"group_id": north["id"]}
            )
        ).json()["items"]
        by_site = (
            await h.client.get(
                f"{API}/mowers", headers=owner.headers, params={"site_id": home["id"]}
            )
        ).json()["items"]
        groups = (await h.client.get(f"{API}/groups", headers=anna.headers)).json()["items"]
        sites = (await h.client.get(f"{API}/sites", headers=anna.headers)).json()["items"]
        site_get = await h.client.get(f"{API}/sites/{home['id']}", headers=anna.headers)
        manage = await h.client.patch(
            f"{API}/groups/{north['id']}", headers=anna.headers, json={"name": "Mine"}
        )
        unknown_site = await h.client.get(f"{API}/mowers/{uuid4()}", headers=owner.headers)
    assert [m["id"] for m in mowers] == [m1]
    assert [m["id"] for m in by_group] == [m1]
    assert [m["id"] for m in by_site] == [m1, m2]
    assert [g["id"] for g in groups] == [north["id"]]
    assert sites == [] and site_get.status_code == 404
    assert manage.status_code == 403  # visible, not manageable
    assert unknown_site.status_code == 404
