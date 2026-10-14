"""HTTP API with minted tokens: registration, lists, commands per kind, cross-account negatives,
bulk commands, sites and groups."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fleet_support import API, FlakyBackend, Harness, fake_gateway, harness
from mowdeck_authz import group, mower, principal_subject, site
from mowdeck_contracts.v1 import (
    MODULE_DESCRIPTOR_PATH,
    CommandPriority,
    EventEnvelope,
    MowerRegistration,
    PrincipalKind,
)
from mowdeck_contracts.v1.subjects import ALL_COMMANDS

from fleet.module import DESCRIPTOR

START = {"command": {"kind": "start_mow", "area_ids": ["front"]}}
STOP = {"command": {"kind": "emergency_stop"}}


def events(h: Harness, event_type: str) -> list[EventEnvelope[MowerRegistration]]:
    """Published ``event.mower.*`` envelopes of one type."""
    subject = f"event.{event_type}"
    return [m.model(EventEnvelope[MowerRegistration]) for m in h.bus.messages(subject)]


async def test_fleet_health_descriptor_and_401() -> None:
    async with harness() as h:
        assert (await h.client.get("/healthz")).json() == {"status": "ok"}
        assert (await h.client.get("/readyz")).status_code == 200
        descriptor = (await h.client.get(MODULE_DESCRIPTOR_PATH)).json()
        anonymous = await h.client.get(f"{API}/mowers")
        forged = await h.client.get(f"{API}/mowers", headers={"Authorization": "Bearer x.y.z"})
    assert descriptor == DESCRIPTOR.model_dump(mode="json")
    keys = {p["key"] for p in descriptor["permissions"]}
    assert {"fleet.manage", "fleet.bulk_command"} <= keys
    assert {e["type"] for e in descriptor["events"]} == {
        "mower.registered",
        "mower.updated",
        "mower.removed",
        "fleet.mower.area_names_changed",
    }
    assert anonymous.status_code == 401
    assert forged.status_code == 401


async def test_fleet_owner_registers_lists_and_gets_a_mower() -> None:
    async with harness() as h:
        owner = await h.owner()
        created = await h.register(
            owner,
            name="Front lawn",
            model="simulator",
            serial="SIM-1",
            connection_options={"time_scale": 30},
        )
        listed = (await h.client.get(f"{API}/mowers", headers=owner.headers)).json()
        got = await h.client.get(f"{API}/mowers/{created['id']}", headers=owner.headers)
        sites = (await h.client.get(f"{API}/sites", headers=owner.headers)).json()
        all_sites = (
            await h.client.get(
                f"{API}/sites", headers=owner.headers, params={"include_default": "true"}
            )
        ).json()
    mower_id = created["id"]
    assert created["name"] == "Front lawn"
    assert created["connection_options"] == {"time_scale": 30}
    assert created["created_by"] == str(owner.principal.subject)
    assert [m["id"] for m in listed["items"]] == [mower_id]
    assert got.status_code == 200 and got.json() == created
    assert sites["items"] == []  # the default site is hidden
    (default,) = all_sites["items"]
    assert default["is_default"] is True and created["site_id"] == default["id"]
    assert h.tuples_on(f"mower:{mower_id}") == {
        f"account@account:{owner.account_id}",
        f"parent@site:{default['id']}",
    }
    assert h.tuples_on(f"site:{default['id']}") == {f"account@account:{owner.account_id}"}
    (registered,) = events(h, "mower.registered")
    assert registered.producer == "fleet"
    assert registered.account_id == owner.account_id
    assert registered.payload.mower_id == UUID(mower_id)
    assert registered.payload.connection_options == {"time_scale": 30}


async def test_fleet_operator_starts_and_the_result_updates_the_record() -> None:
    async with harness() as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        anna = await h.member(owner, "operator")
        mower_id = (await h.register(owner))["id"]
        response = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=anna.headers, json=START
        )
        accepted = response.json()
        record = await h.client.get(
            f"{API}/commands/{accepted['command_id']}", headers=anna.headers
        )
        history = await h.client.get(f"{API}/mowers/{mower_id}/commands", headers=owner.headers)
        create_as_operator = await h.client.post(
            f"{API}/mowers",
            headers=anna.headers,
            json={"name": "x", "connection_kind": "simulated"},
        )
    assert response.status_code == 202, response.text
    assert accepted["status"] == "accepted"
    assert accepted["mower_id"] == mower_id
    assert response.headers["location"] == f"{API}/commands/{accepted['command_id']}"
    (envelope,) = h.commands_published()
    assert envelope.account_id == owner.account_id  # from the mower row
    assert envelope.issued_by.subject == anna.principal.subject  # from the token
    assert envelope.issued_by.kind is PrincipalKind.SUBUSER
    assert envelope.priority is CommandPriority.NORMAL
    assert envelope.expires_at - envelope.issued_at == timedelta(seconds=30)
    body = record.json()
    assert record.status_code == 200
    assert body["status"] == "executed"
    assert body["issued_by"]["subject"] == str(anna.principal.subject)
    assert body["result_at"] is not None
    assert [c["command_id"] for c in history.json()["items"]] == [accepted["command_id"]]
    assert create_as_operator.status_code == 403  # no fleet.manage on the account


async def test_fleet_viewer_cannot_start_but_can_stop_and_refusals_never_reach_the_bus() -> None:
    async with harness() as h:
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        mower_id = (await h.register(owner))["id"]
        start = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=viewer.headers, json=START
        )
        drive = await h.client.post(
            f"{API}/mowers/{mower_id}/commands",
            headers=viewer.headers,
            json={"command": {"kind": "manual_drive", "linear": 0.5, "angular": 0}},
        )
        assert h.bus.messages(ALL_COMMANDS) == []
        assert h.store.tables.commands.rows == {}
        stop = await h.client.post(
            f"{API}/mowers/{mower_id}/commands",
            headers=viewer.headers,
            json={**STOP, "ttl_seconds": 600},
        )
    assert start.status_code == 403
    assert start.headers["content-type"].startswith("application/problem+json")
    assert drive.status_code == 403
    assert stop.status_code == 202, stop.text
    (envelope,) = h.commands_published()
    assert envelope.priority is CommandPriority.EMERGENCY
    assert envelope.expires_at - envelope.issued_at == timedelta(seconds=10)  # short, fixed


async def test_fleet_foreign_owner_gets_404_everywhere() -> None:
    async with harness() as h:
        await fake_gateway(h.bus)
        lukas = await h.owner()
        bob = await h.owner()
        north_site = await h.create(lukas, "sites", name="North")
        north = await h.create(lukas, "groups", name="North lawn", site_id=north_site["id"])
        mower_id = (await h.register(lukas, group_ids=[north["id"]]))["id"]
        command_id = (
            await h.client.post(
                f"{API}/mowers/{mower_id}/commands", headers=lukas.headers, json=START
            )
        ).json()["command_id"]
        published_before = len(h.bus.messages(ALL_COMMANDS))
        routes: list[tuple[str, str, Any]] = [
            ("GET", f"/mowers/{mower_id}", None),
            ("PATCH", f"/mowers/{mower_id}", {"name": "stolen"}),
            ("DELETE", f"/mowers/{mower_id}", None),
            ("PUT", f"/mowers/{mower_id}/groups", {"group_ids": []}),
            ("GET", f"/mowers/{mower_id}/commands", None),
            ("POST", f"/mowers/{mower_id}/commands", START),
            ("POST", f"/mowers/{mower_id}/commands", STOP),
            ("GET", f"/commands/{command_id}", None),
            ("GET", f"/sites/{north_site['id']}", None),
            ("PATCH", f"/sites/{north_site['id']}", {"name": "stolen"}),
            ("DELETE", f"/sites/{north_site['id']}", None),
            ("POST", f"/sites/{north_site['id']}/commands", STOP),
            ("GET", f"/groups/{north['id']}", None),
            ("PATCH", f"/groups/{north['id']}", {"name": "stolen"}),
            ("DELETE", f"/groups/{north['id']}", None),
            ("POST", f"/groups/{north['id']}/commands", START),
            ("POST", f"/groups/{north['id']}/commands", STOP),
        ]
        answers = [
            (
                method,
                path,
                (
                    await h.client.request(method, f"{API}{path}", headers=bob.headers, json=body)
                ).status_code,
            )
            for method, path, body in routes
        ]
        smuggled = await h.client.get(
            f"{API}/mowers", headers=bob.headers, params={"account_id": str(lukas.account_id)}
        )
        bob_sites = await h.client.get(
            f"{API}/sites", headers=bob.headers, params={"include_default": "true"}
        )
        bob_groups = await h.client.get(f"{API}/groups", headers=bob.headers)
        into_foreign_site = await h.client.post(
            f"{API}/mowers",
            headers=bob.headers,
            json={"name": "x", "connection_kind": "simulated", "site_id": north_site["id"]},
        )
        into_foreign_group = await h.client.post(
            f"{API}/mowers",
            headers=bob.headers,
            json={"name": "x", "connection_kind": "simulated", "group_ids": [north["id"]]},
        )
        group_in_foreign_site = await h.client.post(
            f"{API}/groups", headers=bob.headers, json={"name": "x", "site_id": north_site["id"]}
        )
        body_account = await h.client.post(
            f"{API}/mowers",
            headers=bob.headers,
            json={"name": "x", "connection_kind": "simulated", "account_id": str(lukas.account_id)},
        )
        still_there = await h.client.get(f"{API}/mowers/{mower_id}", headers=lukas.headers)
    assert [a for a in answers if a[2] != 404] == []
    assert len(h.bus.messages(ALL_COMMANDS)) == published_before  # nothing reached the bus
    assert smuggled.status_code == 200 and smuggled.json()["items"] == []
    assert bob_sites.json()["items"] == [] and bob_groups.json()["items"] == []
    assert into_foreign_site.status_code == 404
    assert into_foreign_group.status_code == 404
    assert group_in_foreign_site.status_code == 404
    assert body_account.status_code == 422  # unknown fields are refused
    assert still_there.status_code == 200 and still_there.json()["name"] != "stolen"


async def test_fleet_bulk_command_reports_each_visible_mower() -> None:
    async with harness() as h:
        owner = await h.owner()
        home = await h.create(owner, "sites", name="Home")
        north = await h.create(owner, "groups", name="North", site_id=home["id"])
        south = await h.create(owner, "groups", name="South", site_id=home["id"])
        m1 = (await h.register(owner, name="a-one", site_id=home["id"], group_ids=[north["id"]]))[
            "id"
        ]
        m2 = (
            await h.register(
                owner, name="b-two", site_id=home["id"], group_ids=[north["id"], south["id"]]
            )
        )["id"]
        m3 = (await h.register(owner, name="c-three", site_id=home["id"]))["id"]
        anna = await h.member(owner, "viewer", group(north["id"]))  # sees m1 and m2
        await h.identity.replace_subject_grants(  # and may control and bulk-command in South
            principal_subject(anna.principal),
            group(south["id"]),
            ["mower.view", "mower.control", "fleet.bulk_command"],
        )
        manager = await h.member(owner, "manager", site(home["id"]))
        start_north = await h.client.post(
            f"{API}/groups/{north['id']}/commands", headers=anna.headers, json=START
        )
        stop_north = await h.client.post(
            f"{API}/groups/{north['id']}/commands", headers=anna.headers, json=STOP
        )
        published_after_stop = len(h.commands_published())
        start_south = await h.client.post(
            f"{API}/groups/{south['id']}/commands", headers=anna.headers, json=START
        )
        stop_site_as_anna = await h.client.post(
            f"{API}/sites/{home['id']}/commands", headers=anna.headers, json=STOP
        )
        start_site = await h.client.post(
            f"{API}/sites/{home['id']}/commands", headers=manager.headers, json=START
        )
    assert start_north.status_code == 403  # no bulk right on North
    stop = stop_north.json()
    assert stop_north.status_code == 202
    assert [(i["mower_id"], i["outcome"]) for i in stop["items"]] == [
        (m1, "accepted"),
        (m2, "accepted"),
    ]
    assert published_after_stop == 2
    south_items = start_south.json()["items"]
    assert [(i["mower_id"], i["outcome"]) for i in south_items] == [(m2, "accepted")]
    assert stop_site_as_anna.status_code == 404  # Anna does not see the site
    site_items = start_site.json()["items"]
    assert [(i["mower_id"], i["outcome"]) for i in site_items] == [
        (m1, "accepted"),
        (m2, "accepted"),
        (m3, "accepted"),
    ]
    assert len({i["command_id"] for i in site_items}) == 3
    records = [r for r in h.store.tables.commands.rows.values() if r.bulk_id is not None]
    assert {str(r.bulk_id) for r in records} == {
        stop["bulk_id"],
        start_south.json()["bulk_id"],
        start_site.json()["bulk_id"],
    }


async def test_fleet_bulk_marks_visible_but_forbidden_mowers() -> None:
    async with harness() as h:
        owner = await h.owner()
        home = await h.create(owner, "sites", name="Home")
        m1 = (await h.register(owner, name="a", site_id=home["id"]))["id"]
        m2 = (await h.register(owner, name="b", site_id=home["id"]))["id"]
        m3 = (await h.register(owner, name="c", site_id=home["id"]))["id"]
        bulk = await h.member(owner, "viewer", site(home["id"]))  # sees all three
        subject = principal_subject(bulk.principal)
        await h.identity.replace_subject_grants(
            subject, site(home["id"]), ["mower.view", "fleet.bulk_command"]
        )
        await h.identity.replace_subject_grants(subject, mower(m2), ["mower.view", "mower.control"])
        result = await h.client.post(
            f"{API}/sites/{home['id']}/commands", headers=bulk.headers, json=START
        )
    items = result.json()["items"]
    assert result.status_code == 202
    assert [(i["mower_id"], i["outcome"]) for i in items] == [
        (m1, "forbidden"),
        (m2, "accepted"),
        (m3, "forbidden"),
    ]
    assert [e.mower_id for e in h.commands_published()] == [UUID(m2)]


async def test_fleet_sites_and_groups_lifecycle() -> None:
    async with harness() as h:
        owner = await h.owner()
        c = h.client
        hdr = owner.headers
        home = await h.create(
            owner, "sites", name="Home", timezone="Europe/Vienna", address={"city": "Graz"}
        )
        dup = await c.post(f"{API}/sites", headers=hdr, json={"name": "home"})
        bad_tz = await c.post(
            f"{API}/sites", headers=hdr, json={"name": "X", "timezone": "Nowhere"}
        )
        north = await h.create(owner, "groups", name="North", site_id=home["id"])
        cross = await h.create(owner, "groups", name="Everywhere")
        dup_group = await c.post(f"{API}/groups", headers=hdr, json={"name": "NORTH"})
        mower_id = (await h.register(owner, site_id=home["id"], group_ids=[north["id"]]))["id"]
        site_busy = await c.delete(f"{API}/sites/{home['id']}", headers=hdr)
        group_busy = await c.delete(f"{API}/groups/{north['id']}", headers=hdr)
        got_group = (await c.get(f"{API}/groups/{north['id']}", headers=hdr)).json()
        listed = (await c.get(f"{API}/groups", headers=hdr, params={"site_id": home["id"]})).json()
        renamed = await c.patch(
            f"{API}/sites/{home['id']}", headers=hdr, json={"name": "Garden", "address": None}
        )
        moved = await c.patch(f"{API}/groups/{north['id']}", headers=hdr, json={"site_id": None})
        parent_after_move = h.tuples_on(f"group:{north['id']}")
        regrouped = await c.put(
            f"{API}/mowers/{mower_id}/groups", headers=hdr, json={"group_ids": [cross["id"]]}
        )
        mower_parents = h.tuples_on(f"mower:{mower_id}")
        deleted_group = await c.delete(f"{API}/groups/{north['id']}", headers=hdr)
        default_id = (await h.register(owner))["site_id"]  # creates the default site
        default_listed = (
            await c.get(f"{API}/sites", headers=hdr, params={"include_default": "true"})
        ).json()["items"]
        rename_default = await c.patch(
            f"{API}/sites/{default_id}", headers=hdr, json={"name": "Mine"}
        )
        tz_default = await c.patch(
            f"{API}/sites/{default_id}", headers=hdr, json={"timezone": "UTC"}
        )
        delete_default = await c.delete(f"{API}/sites/{default_id}", headers=hdr)
        moved_mower = await c.patch(
            f"{API}/mowers/{mower_id}", headers=hdr, json={"site_id": default_id}
        )
        deleted_mower = await c.delete(f"{API}/mowers/{mower_id}", headers=hdr)
        deleted_cross = await c.delete(f"{API}/groups/{cross['id']}", headers=hdr)
        deleted_site = await c.delete(f"{API}/sites/{home['id']}", headers=hdr)
        gone = await c.get(f"{API}/sites/{home['id']}", headers=hdr)
    assert home["address"] == {"street": None, "postal_code": None, "city": "Graz", "country": None}
    assert dup.status_code == 409 and bad_tz.status_code == 422 and dup_group.status_code == 409
    assert cross["site_id"] is None
    assert h.tuples_on(f"group:{cross['id']}") == set()  # deleted at the end
    assert site_busy.status_code == 409 and group_busy.status_code == 409
    assert got_group["mower_ids"] == [mower_id]
    assert [g["id"] for g in listed["items"]] == [north["id"]]
    assert renamed.json()["name"] == "Garden" and renamed.json()["address"] is None
    assert moved.json()["site_id"] is None
    assert parent_after_move == {f"account@account:{owner.account_id}"}
    assert regrouped.json()["group_ids"] == [cross["id"]]
    assert f"parent@group:{cross['id']}" in mower_parents
    assert f"parent@group:{north['id']}" not in mower_parents
    assert deleted_group.status_code == 204
    assert [x["id"] for x in default_listed if x["is_default"]] == [default_id]
    assert rename_default.status_code == 409
    assert tz_default.status_code == 200 and tz_default.json()["timezone"] == "UTC"
    assert delete_default.status_code == 409
    assert moved_mower.json()["site_id"] == default_id
    assert deleted_mower.status_code == 204 and deleted_cross.status_code == 204
    assert deleted_site.status_code == 204 and gone.status_code == 404
    assert h.tuples_on(f"mower:{mower_id}") == set()
    assert h.tuples_on(f"site:{home['id']}") == set()
    actions = [e.action for e in h.store.tables.audit.rows.values()]
    assert actions[:3] == ["site.created", "group.created", "group.created"]
    assert "mower.moved" in actions and actions[-1] == "site.deleted"


async def test_fleet_mower_updates_publish_events_and_validate() -> None:
    async with harness() as h:
        owner = await h.owner()
        c, hdr = h.client, owner.headers
        mower_id = (await h.register(owner, serial="S-1"))["id"]
        other = (await h.register(owner, serial="S-2"))["id"]
        model_only = await c.patch(
            f"{API}/mowers/{mower_id}", headers=hdr, json={"model": "LUBA 2"}
        )
        updates_after_model = len(events(h, "mower.updated"))
        renamed = await c.patch(f"{API}/mowers/{mower_id}", headers=hdr, json={"name": "Back"})
        options = await c.patch(
            f"{API}/mowers/{mower_id}",
            headers=hdr,
            json={"connection_options": {"battery_percent": 50}},
        )
        dup_serial = await c.patch(f"{API}/mowers/{other}", headers=hdr, json={"serial": "S-1"})
        dup_register = await c.post(
            f"{API}/mowers",
            headers=hdr,
            json={"name": "x", "connection_kind": "simulated", "serial": "S-1"},
        )
        wrong_kind = await c.post(
            f"{API}/mowers", headers=hdr, json={"name": "x", "connection_kind": "cloud"}
        )
        secret = await c.post(
            f"{API}/mowers",
            headers=hdr,
            json={
                "name": "x",
                "connection_kind": "simulated",
                "connection_options": {"password": "p"},
            },
        )
        unknown_group = await c.post(
            f"{API}/mowers",
            headers=hdr,
            json={"name": "x", "connection_kind": "simulated", "group_ids": [str(uuid4())]},
        )
        deleted = await c.delete(f"{API}/mowers/{mower_id}", headers=hdr)
        again = await c.get(f"{API}/mowers/{mower_id}", headers=hdr)
    assert model_only.status_code == 200 and model_only.json()["model"] == "LUBA 2"
    assert updates_after_model == 0  # the registration did not change
    assert renamed.json()["name"] == "Back"
    assert options.json()["connection_options"] == {"battery_percent": 50}
    updated = events(h, "mower.updated")
    assert [e.payload.name for e in updated] == ["Back", "Back"]
    assert updated[-1].payload.connection_options == {"battery_percent": 50}
    assert dup_serial.status_code == 409 and dup_register.status_code == 409
    assert wrong_kind.status_code == 422 and secret.status_code == 422
    assert unknown_group.status_code == 404
    assert deleted.status_code == 204 and again.status_code == 404
    (removed,) = events(h, "mower.removed")
    assert removed.payload.mower_id == UUID(mower_id)


async def test_fleet_mower_quota() -> None:
    async with harness(max_mowers_per_account=1) as h:
        owner = await h.owner()
        await h.register(owner)
        second = await h.client.post(
            f"{API}/mowers",
            headers=owner.headers,
            json={"name": "x", "connection_kind": "simulated"},
        )
    assert second.status_code == 409
    assert "limit" in second.json()["detail"]


async def test_fleet_command_history_pages_and_unknown_records() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = (await h.register(owner))["id"]
        for _ in range(3):
            response = await h.client.post(
                f"{API}/mowers/{mower_id}/commands",
                headers=owner.headers,
                json={"command": {"kind": "pause_mow"}},
            )
            assert response.status_code == 202
        first = (
            await h.client.get(
                f"{API}/mowers/{mower_id}/commands", headers=owner.headers, params={"limit": 2}
            )
        ).json()
        rest = (
            await h.client.get(
                f"{API}/mowers/{mower_id}/commands",
                headers=owner.headers,
                params={"limit": 2, "before": first["next_before"]},
            )
        ).json()
        unknown = await h.client.get(f"{API}/commands/{uuid4()}", headers=owner.headers)
        not_a_uuid = await h.client.get(f"{API}/mowers/not-a-uuid", headers=owner.headers)
    assert len(first["items"]) == 2 and first["next_before"] is not None
    assert all(item["status"] == "pending" for item in first["items"])  # no gateway here
    assert len(rest["items"]) <= 1
    assert unknown.status_code == 404
    assert not_a_uuid.status_code == 404


async def test_fleet_command_without_bus_is_503_and_recorded_as_failed() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = (await h.register(owner))["id"]
        await h.bus.close()
        response = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=owner.headers, json=START
        )
    assert response.status_code == 503
    (record,) = h.store.tables.commands.rows.values()
    assert record.status == "failed"
    assert record.reason is not None and "not published" in record.reason


async def test_fleet_openfga_outage_is_503_and_changes_nothing() -> None:
    async with harness(backend=FlakyBackend) as h:
        owner = await h.owner()
        h.backend.fail_writes = True
        response = await h.client.post(
            f"{API}/mowers",
            headers=owner.headers,
            json={"name": "x", "connection_kind": "simulated"},
        )
    assert response.status_code == 503
    assert h.store.tables.mowers.rows == {} and h.store.tables.sites.rows == {}
    assert events(h, "mower.registered") == []


@pytest.mark.parametrize("kind", [PrincipalKind.SERVICE])
async def test_fleet_service_principals_have_no_object_rights(kind: PrincipalKind) -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = (await h.register(owner))["id"]
        service = h.person(owner.account_id, kind=kind)
        got = await h.client.get(f"{API}/mowers/{mower_id}", headers=service.headers)
        listed = await h.client.get(f"{API}/mowers", headers=service.headers)
        stop = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=service.headers, json=STOP
        )
    assert got.status_code == 404
    assert listed.json()["items"] == []
    assert stop.status_code == 404
