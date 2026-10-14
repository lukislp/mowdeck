"""Bus interfaces: ``fleet.mowers.list`` with shards, account events, cache invalidation."""

from uuid import UUID, uuid4

from fleet_support import harness
from mowdeck_authz import Subject
from mowdeck_contracts.v1 import (
    IDENTITY_ACCOUNT_CREATED,
    IDENTITY_GRANTS_CHANGED,
    AccountCreated,
    AccountKind,
    EventEnvelope,
    GrantsChanged,
    MowerListReply,
    MowerListRequest,
)
from mowdeck_contracts.v1.subjects import FLEET_MOWERS_LIST, command_result_subject
from mowdeck_service_kit.bus import BusMessage, MessageHandler, publish_event

from fleet.adapters.bus.subscriptions import LIST_QUEUE, list_handler


async def test_fleet_answers_mower_list_requests_per_shard() -> None:
    async with harness() as h:
        owner = await h.owner()
        other = await h.owner()
        ids = {UUID((await h.register(owner))["id"]) for _ in range(5)}
        ids.add(UUID((await h.register(other))["id"]))
        replies = []
        for index in range(2):
            reply = await h.bus.request(
                FLEET_MOWERS_LIST, MowerListRequest(shard_index=index, shard_count=2)
            )
            replies.append(reply.model(MowerListReply).mowers)
        everything = (await h.bus.request(FLEET_MOWERS_LIST, MowerListRequest())).model(
            MowerListReply
        )
    shard_sets = [{r.mower_id for r in mowers} for mowers in replies]
    assert shard_sets[0] | shard_sets[1] == ids
    assert not shard_sets[0] & shard_sets[1]
    assert all(m.int % 2 == i for i, s in enumerate(shard_sets) for m in s)
    assert {r.mower_id for r in everything.mowers} == ids
    accounts = {r.account_id for r in everything.mowers}
    assert accounts == {owner.account_id, other.account_id}  # system path, all accounts


async def test_fleet_mower_list_is_answered_by_one_replica() -> None:
    async with harness() as h:
        await h.register(await h.owner())
        calls: list[str] = []
        inner = list_handler(h.app.state.fleet)

        def replica(name: str) -> MessageHandler:
            async def handle(message: BusMessage) -> None:
                calls.append(name)
                await inner(message)

            return handle

        for name in ("b", "c"):
            await h.bus.subscribe(FLEET_MOWERS_LIST, replica(name), queue=LIST_QUEUE)
        for _ in range(6):
            reply = await h.bus.request(FLEET_MOWERS_LIST, MowerListRequest())
            assert len(reply.model(MowerListReply).mowers) == 1
        await h.bus.publish(FLEET_MOWERS_LIST, b"not json")  # dropped, no answer expected
    assert len(calls) == 4  # 6 requests, each to one of 3 members (the app, b, c)
    assert set(calls) == {"b", "c"}


async def test_fleet_creates_the_default_site_on_account_created() -> None:
    async with harness() as h:
        account_id = uuid4()
        event = EventEnvelope[AccountCreated](
            type=IDENTITY_ACCOUNT_CREATED,
            producer="identity",
            account_id=account_id,
            payload=AccountCreated(
                account_id=account_id,
                name="Home",
                kind=AccountKind.HOUSEHOLD,
                owner_user_id=uuid4(),
            ),
        )
        await publish_event(h.bus, event)
        await publish_event(h.bus, event.model_copy(update={"id": uuid4()}))  # redelivered
        await h.bus.publish("event.identity.account.created", b"{}", msg_id="bad")
    sites = [s for s in h.store.tables.sites.rows.values() if s.account_id == account_id]
    assert len(sites) == 1 and sites[0].is_default
    assert h.tuples_on(f"site:{sites[0].id}") == {f"account@account:{account_id}"}
    assert [m.msg_id for m in h.bus.dropped] == ["bad"]


async def test_fleet_drops_cached_decisions_on_grants_changed() -> None:
    async with harness() as h:
        dropped: list[str] = []

        def drop(subject: Subject | str) -> None:
            dropped.append(str(subject))

        h.authz.invalidate_subject = drop  # type: ignore[method-assign]
        subject = f"user:{uuid4()}"
        await publish_event(
            h.bus,
            EventEnvelope[GrantsChanged](
                type=IDENTITY_GRANTS_CHANGED,
                producer="identity",
                payload=GrantsChanged(subjects=[subject]),
            ),
        )
    assert dropped == [subject]


async def test_fleet_ignores_malformed_command_results() -> None:
    async with harness() as h:
        await h.bus.publish(command_result_subject(uuid4()), b"{}", msg_id="poison")
    assert [m.msg_id for m in h.bus.dropped] == ["poison"]
