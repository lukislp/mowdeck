"""Internal requests of other services: ``fleet.commands.issue``, ``fleet.targets.resolve``, and
the site coordinates they read."""

from uuid import UUID, uuid4

from fleet_support import API, fake_gateway, harness
from mowdeck_authz import group
from mowdeck_contracts.v1 import (
    CommandRequest,
    IssueCommandErrorCode,
    IssueCommandReply,
    IssueCommandRequest,
    PrincipalKind,
    PrincipalRef,
    ResolveTargetReply,
    ResolveTargetRequest,
    ReturnToDock,
    StartMow,
)
from mowdeck_contracts.v1.subjects import FLEET_COMMANDS_ISSUE, FLEET_TARGETS_RESOLVE
from mowdeck_service_kit.bus import InMemoryBus


def _issue(
    subject: UUID,
    account_id: UUID,
    mower_id: UUID,
    *,
    kind: PrincipalKind = PrincipalKind.USER,
    schedule_id: UUID | None = None,
) -> IssueCommandRequest:
    return IssueCommandRequest(
        on_behalf_of=PrincipalRef(subject=subject, kind=kind),
        account_id=account_id,
        mower_id=mower_id,
        command=CommandRequest(command=StartMow(area_ids=["front"], pattern={"angle": 30})),
        schedule_id=schedule_id,
    )


async def _ask(bus: InMemoryBus, request: IssueCommandRequest) -> IssueCommandReply:
    return (await bus.request(FLEET_COMMANDS_ISSUE, request)).model(IssueCommandReply)


async def test_fleet_issue_round_trip_records_origin_and_schedule() -> None:
    async with harness() as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        mower_id = UUID((await h.register(owner))["id"])
        schedule_id = uuid4()
        reply = await _ask(
            h.bus,
            _issue(owner.principal.subject, owner.account_id, mower_id, schedule_id=schedule_id),
        )
        assert reply.error is None and reply.accepted is not None
        record = await h.client.get(
            f"{API}/commands/{reply.accepted.command_id}", headers=owner.headers
        )
    assert record.status_code == 200
    body = record.json()
    assert body["origin"] == "scheduler"
    assert body["schedule_id"] == str(schedule_id)
    assert body["status"] == "executed"
    assert body["issued_by"]["subject"] == str(owner.principal.subject)
    [published] = h.commands_published()
    assert published.issued_by.subject == owner.principal.subject
    assert published.command.kind == "start_mow"


async def test_fleet_issue_refuses_a_subject_without_can_control() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = UUID((await h.register(owner))["id"])
        viewer = await h.member(owner, "viewer")
        stranger = await h.member(await h.owner(), "operator")
        forbidden = await _ask(h.bus, _issue(viewer.principal.subject, owner.account_id, mower_id))
        hidden = await _ask(h.bus, _issue(stranger.principal.subject, owner.account_id, mower_id))
        foreign = await _ask(h.bus, _issue(owner.principal.subject, stranger.account_id, mower_id))
        service = await _ask(
            h.bus,
            _issue(owner.principal.subject, owner.account_id, mower_id, kind=PrincipalKind.SERVICE),
        )
        records = list(h.store.tables.commands.rows.values())
    assert forbidden.error is not None and forbidden.error.code == IssueCommandErrorCode.FORBIDDEN
    assert hidden.error is not None and hidden.error.code == IssueCommandErrorCode.NOT_FOUND
    assert foreign.error is not None and foreign.error.code == IssueCommandErrorCode.NOT_FOUND
    assert service.error is not None and service.error.code == IssueCommandErrorCode.INVALID
    assert h.commands_published() == []  # nothing reached the bus
    assert records == []  # and nothing was recorded


async def test_fleet_issue_operator_on_a_group_may_dock_its_mowers() -> None:
    async with harness() as h:
        owner = await h.owner()
        created = await h.create(owner, "groups", name="North")
        mower_id = UUID((await h.register(owner, group_ids=[created["id"]]))["id"])
        operator = await h.member(owner, "operator", group(UUID(created["id"])))
        request = IssueCommandRequest(
            on_behalf_of=PrincipalRef(
                subject=operator.principal.subject, kind=PrincipalKind.SUBUSER
            ),
            account_id=owner.account_id,
            mower_id=mower_id,
            command=CommandRequest(command=ReturnToDock()),
        )
        reply = await _ask(h.bus, request)
    assert reply.accepted is not None and reply.accepted.kind == "return_to_dock"


async def test_fleet_issue_answers_unavailable_when_the_bus_rejects_commands() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = UUID((await h.register(owner))["id"])

        class Broken:
            async def publish(self, envelope: object) -> None:
                raise ConnectionError("down")

        h.app.state.fleet.commands.publisher = Broken()
        reply = await _ask(h.bus, _issue(owner.principal.subject, owner.account_id, mower_id))
        [record] = h.store.tables.commands.rows.values()
    assert reply.error is not None and reply.error.code == IssueCommandErrorCode.UNAVAILABLE
    assert record.status == "failed" and record.origin == "scheduler"


async def test_fleet_issue_drops_malformed_requests() -> None:
    async with harness() as h:
        from mowdeck_service_kit.bus import RequestTimeoutError

        try:
            await h.bus.request(FLEET_COMMANDS_ISSUE, b"{}", timeout=0.2)
        except RequestTimeoutError:
            pass
        else:  # pragma: no cover - the in-memory bus answers nothing for a dropped request
            raise AssertionError("expected no reply")


async def test_fleet_resolve_target_lists_mowers_with_site_places() -> None:
    async with harness() as h:
        owner = await h.owner()
        site = await h.create(
            owner, "sites", name="Garden", timezone="Europe/Vienna", latitude=48.2, longitude=16.4
        )
        site_group = await h.create(owner, "groups", name="North", site_id=site["id"])
        cross = await h.create(owner, "groups", name="All")
        first = UUID(
            (
                await h.register(
                    owner, site_id=site["id"], group_ids=[site_group["id"], cross["id"]]
                )
            )["id"]
        )
        second = UUID((await h.register(owner, group_ids=[cross["id"]]))["id"])

        async def resolve(
            target_type: str, target_id: UUID, account_id: UUID
        ) -> ResolveTargetReply:
            reply = await h.bus.request(
                FLEET_TARGETS_RESOLVE,
                ResolveTargetRequest(
                    account_id=account_id,
                    target_type=target_type,  # type: ignore[arg-type]
                    target_id=target_id,
                ),
            )
            return reply.model(ResolveTargetReply)

        one = await resolve("mower", first, owner.account_id)
        grouped = await resolve("group", UUID(site_group["id"]), owner.account_id)
        everywhere = await resolve("group", UUID(cross["id"]), owner.account_id)
        foreign = await resolve("mower", first, uuid4())
        missing = await resolve("group", uuid4(), owner.account_id)
    assert one.found and one.timezone == "Europe/Vienna"
    assert [(m.mower_id, m.latitude, m.longitude) for m in one.mowers] == [(first, 48.2, 16.4)]
    assert grouped.timezone == "Europe/Vienna" and [m.mower_id for m in grouped.mowers] == [first]
    assert everywhere.timezone == "Europe/Berlin"  # cross-site: the default site
    assert {m.mower_id for m in everywhere.mowers} == {first, second}
    second_place = next(m for m in everywhere.mowers if m.mower_id == second)
    assert second_place.latitude is None
    assert not foreign.found and not missing.found


async def test_fleet_site_coordinates_are_validated_and_editable() -> None:
    async with harness() as h:
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        bad = await h.client.post(
            f"{API}/sites",
            headers=owner.headers,
            json={"name": "A", "latitude": 91, "longitude": 1},
        )
        half = await h.client.post(
            f"{API}/sites", headers=owner.headers, json={"name": "A", "latitude": 50}
        )
        site = await h.create(owner, "sites", name="Garden")
        assert site["latitude"] is None
        url = f"{API}/sites/{site['id']}"
        changed = await h.client.patch(
            url, headers=owner.headers, json={"latitude": 52.52, "longitude": 13.4}
        )
        refused = await h.client.patch(url, headers=viewer.headers, json={"latitude": 1.0})
        half_patch = await h.client.patch(url, headers=owner.headers, json={"longitude": None})
        cleared = await h.client.patch(
            url, headers=owner.headers, json={"latitude": None, "longitude": None}
        )
    assert bad.status_code == 422 and half.status_code == 422
    assert changed.status_code == 200
    assert (changed.json()["latitude"], changed.json()["longitude"]) == (52.52, 13.4)
    assert refused.status_code == 403
    assert half_patch.status_code == 422
    assert cleared.json()["latitude"] is None and cleared.json()["longitude"] is None
