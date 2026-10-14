"""HTTP API: access rules, cross-account isolation, history bins, bounds."""

from datetime import timedelta
from uuid import uuid4

import pytest
from mowdeck_authz import RelationshipTuple, account, group, mower, principal_subject
from mowdeck_contracts.v1 import (
    MODULE_DESCRIPTOR_PATH,
    CommandStatus,
    MowerActivity,
    Principal,
    PrincipalKind,
)
from telemetry_support import API, T0, Person, harness, make_state, seconds

from telemetry.module import DESCRIPTOR, VIEW_PERMISSION

ISO = "%Y-%m-%dT%H:%M:%SZ"


def window(start_offset: float = -1, end_offset: float = 3600) -> dict[str, str]:
    """Query parameters for a range around ``T0``."""
    return {
        "from": (T0 + seconds(start_offset)).strftime(ISO),
        "to": (T0 + seconds(end_offset)).strftime(ISO),
    }


async def test_telemetry_health_and_module_descriptor() -> None:
    async with harness() as h:
        assert (await h.client.get("/healthz")).json() == {"status": "ok"}
        ready = await h.client.get("/readyz")
        descriptor = (await h.client.get(MODULE_DESCRIPTOR_PATH)).json()
    assert ready.status_code == 200
    assert descriptor == DESCRIPTOR.model_dump(mode="json")
    key = descriptor["permissions"][0]
    assert (key["key"], key["implied_by"]) == (VIEW_PERMISSION, ["mower.view"])


async def test_telemetry_requires_a_token() -> None:
    async with harness() as h:
        for path in (f"mowers/{uuid4()}/state", "accounts/me/mowers/states"):
            response = await h.client.get(f"{API}/{path}")
            assert response.status_code == 401
            assert response.headers["content-type"].startswith("application/problem+json")


async def feed(h, owner: Person, mower_id) -> None:  # type: ignore[no-untyped-def]
    """A short mowing session: states, a position track and a command."""
    await h.publish_state(make_state(mower_id, owner.account_id, at=T0, position=(48.1, 11.5)))
    for i in range(1, 7):
        await h.publish_state(
            make_state(
                mower_id,
                owner.account_id,
                at=T0 + seconds(i),
                activity=MowerActivity.MOWING,
                battery=90 - i,
                position=(48.1 + i / 10_000, 11.5),
            )
        )
    command_id = await h.publish_result(mower_id, CommandStatus.ACCEPTED, at=T0 + seconds(1))
    await h.publish_result(
        mower_id, CommandStatus.EXECUTED, command_id=command_id, at=T0 + seconds(2)
    )


async def test_telemetry_owner_reads_state_history_positions_and_commands() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await feed(h, owner, mower_id)
        base = f"{API}/mowers/{mower_id}"
        state = await h.client.get(f"{base}/state", headers=owner.headers)
        history = await h.client.get(
            f"{base}/history", headers=owner.headers, params={**window(), "step": "5s"}
        )
        raw = await h.client.get(f"{base}/history", headers=owner.headers, params=window())
        positions = await h.client.get(f"{base}/positions", headers=owner.headers, params=window())
        commands = await h.client.get(f"{base}/commands", headers=owner.headers)
    assert state.status_code == 200, state.text
    body = state.json()
    assert body["mower_id"] == str(mower_id) and isinstance(body["stale"], bool)
    assert body["state"]["activity"] == "mowing" and body["state"]["battery_percent"] == 84
    assert history.status_code == 200, history.text
    bins = history.json()
    assert bins["step_seconds"] == 5 and bins["truncated"] is False
    assert [p["samples"] for p in bins["points"]] == [5, 2]
    assert bins["points"][0]["activity"] == "mowing" and bins["points"][0]["lat"] is not None
    assert raw.status_code == 200 and raw.json()["step_seconds"] is None
    assert len(raw.json()["points"]) == 7
    assert positions.status_code == 200
    track = positions.json()["positions"]
    assert len(track) == 7 and track[0]["lat"] == 48.1 and track[-1]["rtk_fix"] == "fixed"
    assert commands.status_code == 200
    assert [i["status"] for i in commands.json()["items"]] == ["executed", "accepted"]


async def test_telemetry_a_foreign_owner_gets_404_on_every_mower_route() -> None:
    async with harness() as h:
        owner = await h.owner()
        intruder = await h.owner()
        mower_id = await h.add_mower(owner)
        await feed(h, owner, mower_id)
        base = f"{API}/mowers/{mower_id}"
        for suffix, params in (
            ("state", {}),
            ("history", {**window(), "step": "5s"}),
            ("positions", window()),
            ("commands", {}),
        ):
            own = await h.client.get(f"{base}/{suffix}", headers=owner.headers, params=params)
            foreign = await h.client.get(
                f"{base}/{suffix}", headers=intruder.headers, params=params
            )
            smuggled = await h.client.get(
                f"{base}/{suffix}",
                headers=intruder.headers,
                params={**params, "account_id": str(owner.account_id)},
            )
            assert own.status_code == 200, (suffix, own.text)
            assert foreign.status_code == 404, suffix
            assert smuggled.status_code == 404, suffix
            assert foreign.json()["detail"] != "no telemetry yet"
        # an unknown mower looks exactly like a foreign one
        unknown = await h.client.get(f"{API}/mowers/{uuid4()}/state", headers=owner.headers)
        assert unknown.status_code == 404


async def test_telemetry_a_visible_mower_without_telemetry_is_404_no_telemetry_yet() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        response = await h.client.get(f"{API}/mowers/{mower_id}/state", headers=owner.headers)
        history = await h.client.get(
            f"{API}/mowers/{mower_id}/history", headers=owner.headers, params=window()
        )
        commands = await h.client.get(f"{API}/mowers/{mower_id}/commands", headers=owner.headers)
    assert response.status_code == 404
    problem = response.json()
    assert problem["detail"] == "no telemetry yet"
    assert problem["type"] == "urn:mowdeck:problem:no-telemetry-yet"
    assert history.status_code == 200 and history.json()["points"] == []
    assert commands.status_code == 200 and commands.json()["items"] == []


async def grant_view(h, person: Person, obj) -> None:  # type: ignore[no-untyped-def]
    await h.identity.replace_subject_grants(
        principal_subject(person.principal), obj, ["mower.view"]
    )


async def test_telemetry_states_of_the_account_list_only_visible_mowers() -> None:
    async with harness() as h:
        owner = await h.owner()
        other = await h.owner()
        mine = [await h.add_mower(owner) for _ in range(3)]
        theirs = await h.add_mower(other)
        for m in mine:
            await h.publish_state(make_state(m, owner.account_id))
        await h.publish_state(make_state(theirs, other.account_id))
        await h.add_mower(owner)  # visible, no telemetry yet: absent from the list

        # a sub-user who may view one mower only (grant on the mower itself)
        sub = Principal(subject=uuid4(), account_id=owner.account_id, kind=PrincipalKind.SUBUSER)
        sub_person = Person(sub, h.keys.mint(sub))
        await h.identity.write(
            [RelationshipTuple(principal_subject(sub), "member", account(owner.account_id))]
        )
        await grant_view(h, sub_person, mower(mine[0]))

        everything = await h.client.get(f"{API}/accounts/me/mowers/states", headers=owner.headers)
        partial = await h.client.get(f"{API}/accounts/me/mowers/states", headers=sub_person.headers)
        foreign = await h.client.get(f"{API}/accounts/me/mowers/states", headers=other.headers)
    assert {i["mower_id"] for i in everything.json()["items"]} == {str(m) for m in mine}
    assert [i["mower_id"] for i in partial.json()["items"]] == [str(mine[0])]
    assert [i["mower_id"] for i in foreign.json()["items"]] == [str(theirs)]
    assert everything.json()["truncated"] is False


async def test_telemetry_a_group_viewer_sees_only_that_groups_mowers() -> None:
    async with harness() as h:
        owner = await h.owner()
        inside, outside = await h.add_mower(owner), await h.add_mower(owner)
        gid = uuid4()
        await h.fleet.write(
            [
                RelationshipTuple(account(owner.account_id).as_subject(), "account", group(gid)),
                RelationshipTuple(group(gid).as_subject(), "parent", mower(inside)),
            ]
        )
        sub = Principal(subject=uuid4(), account_id=owner.account_id, kind=PrincipalKind.SUBUSER)
        person = Person(sub, h.keys.mint(sub))
        await h.identity.write(
            [RelationshipTuple(principal_subject(sub), "member", account(owner.account_id))]
        )
        await grant_view(h, person, group(gid))
        for m in (inside, outside):
            await h.publish_state(make_state(m, owner.account_id))
        ok = await h.client.get(f"{API}/mowers/{inside}/state", headers=person.headers)
        hidden = await h.client.get(f"{API}/mowers/{outside}/state", headers=person.headers)
    assert ok.status_code == 200 and hidden.status_code == 404


async def test_telemetry_states_list_is_bounded() -> None:
    async with harness(telemetry_states_limit=2) as h:
        owner = await h.owner()
        for _ in range(3):
            m = await h.add_mower(owner)
            await h.publish_state(make_state(m, owner.account_id))
        response = await h.client.get(f"{API}/accounts/me/mowers/states", headers=owner.headers)
    assert len(response.json()["items"]) == 2 and response.json()["truncated"] is True


async def test_telemetry_state_is_not_stale_when_recent() -> None:
    now = T0 + seconds(30)
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.publish_state(make_state(mower_id, owner.account_id, at=T0))
        h.app.state.telemetry.queries._clock = lambda: now
        fresh = await h.client.get(f"{API}/mowers/{mower_id}/state", headers=owner.headers)
        h.app.state.telemetry.queries._clock = lambda: T0 + timedelta(minutes=5)
        stale = await h.client.get(f"{API}/mowers/{mower_id}/state", headers=owner.headers)
    assert fresh.json()["stale"] is False and stale.json()["stale"] is True


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"step": "abc"}, "step must"),
        ({"step": "0s"}, "step must"),
        ({"step": "1s", "from": "2026-10-01T00:00:00Z", "to": "2026-10-09T00:00:00Z"}, "too small"),
        ({"from": "2026-10-09T00:00:00Z", "to": "2026-10-01T00:00:00Z"}, "before"),
        ({"from": "2026-01-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"}, "at most 31 days"),
    ],
)
async def test_telemetry_history_validates_range_and_step(
    params: dict[str, str], fragment: str
) -> None:
    async with harness(telemetry_query_max_rows=100) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        response = await h.client.get(
            f"{API}/mowers/{mower_id}/history", headers=owner.headers, params=params
        )
    assert response.status_code == 422, response.text
    assert fragment in response.json()["detail"]


async def test_telemetry_history_and_positions_are_truncated_at_the_row_limit() -> None:
    async with harness(telemetry_query_max_rows=10) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        for i in range(15):
            await h.publish_state(
                make_state(
                    mower_id,
                    owner.account_id,
                    at=T0 + seconds(i),
                    battery=90 - i,
                    position=(48.0 + i / 1000, 11.0),
                )
            )
        history = await h.client.get(
            f"{API}/mowers/{mower_id}/history", headers=owner.headers, params=window()
        )
        positions = await h.client.get(
            f"{API}/mowers/{mower_id}/positions",
            headers=owner.headers,
            params={**window(), "limit": 5},
        )
        capped = await h.client.get(
            f"{API}/mowers/{mower_id}/positions", headers=owner.headers, params=window()
        )
    assert len(history.json()["points"]) == 10 and history.json()["truncated"] is True
    track = positions.json()["positions"]
    assert len(track) == 5 and positions.json()["truncated"] is True
    assert track[-1]["lat"] == 48.014  # the newest points, oldest first
    assert len(capped.json()["positions"]) == 10


async def test_telemetry_default_range_is_the_last_day() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        h.app.state.telemetry.queries._clock = lambda: T0 + timedelta(hours=1)
        await h.publish_state(make_state(mower_id, owner.account_id, at=T0))
        response = await h.client.get(f"{API}/mowers/{mower_id}/history", headers=owner.headers)
    body = response.json()
    assert len(body["points"]) == 1
    assert body["from"] == "2026-10-08T13:00:00Z" and body["to"] == "2026-10-09T13:00:00Z"


async def test_telemetry_default_range_with_a_step_fits_the_bin_limit() -> None:
    async with harness(telemetry_query_max_rows=100) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        h.app.state.telemetry.queries._clock = lambda: T0 + timedelta(hours=1)
        await h.publish_state(make_state(mower_id, owner.account_id, at=T0 + timedelta(minutes=59)))
        fine = await h.client.get(
            f"{API}/mowers/{mower_id}/history", headers=owner.headers, params={"step": "5s"}
        )
        coarse = await h.client.get(
            f"{API}/mowers/{mower_id}/history", headers=owner.headers, params={"step": "1h"}
        )
    assert fine.status_code == 200, fine.text  # 100 bins of 5 s: the default range shrinks
    assert fine.json()["to"] == "2026-10-09T13:00:00Z" and fine.json()["from"] > "2026-10-09T12:50"
    assert coarse.status_code == 200 and coarse.json()["from"] == "2026-10-08T13:00:00Z"
