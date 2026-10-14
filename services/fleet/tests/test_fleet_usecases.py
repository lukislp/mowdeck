"""Use cases with the memory store, the OpenFGA fake and the in-memory bus (no HTTP)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fleet_support import FlakyBackend, FlakyStore
from mowdeck_authz import (
    RelationshipTuple,
    TupleOwnershipError,
    TupleWriter,
    account,
    group,
    mower,
    principal_subject,
    user,
)
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    MOWER_REGISTERED,
    CommandRequest,
    CommandResult,
    CommandStatus,
    ConnectionKind,
    EventEnvelope,
    MowerRegistration,
    Principal,
    PrincipalKind,
)
from mowdeck_contracts.v1.subjects import ALL_COMMANDS
from mowdeck_service_kit.bus import InMemoryBus
from pydantic import BaseModel

from fleet.adapters.bus.publisher import BusCommandPublisher, BusEventPublisher
from fleet.adapters.memory import MemoryStore
from fleet.application.context import Actor, FleetConfig
from fleet.application.mowers import MowerInput
from fleet.application.services import Services, build_services
from fleet.domain.errors import ConflictError, ForbiddenError, NotFoundError, Unavailable
from fleet.domain.models import CommandRecord


class Clock:
    """A wall clock tests can move."""

    def __init__(self) -> None:
        # Real time: the bus publisher checks command expiry against the wall clock, so a
        # fixed date here turns into a time bomb once that date has passed.
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now


class SpyEvents:
    """Records events and whether the store was inside a transaction at publish time."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.published: list[tuple[str, UUID, BaseModel, bool]] = []

    async def publish(self, event_type: str, account_id: UUID, payload: BaseModel) -> None:
        self.published.append((event_type, account_id, payload, self.store._lock.locked()))


@dataclass
class World:
    """Services over fakes plus an owner with a fresh account."""

    services: Services
    store: MemoryStore
    authz: InMemoryAuthorizer
    bus: InMemoryBus
    events: SpyEvents
    clock: Clock
    owner: Actor
    account_id: UUID

    async def member(self, keys: list[str], level: Any = None) -> Actor:
        principal = Principal(
            subject=uuid4(), account_id=self.account_id, kind=PrincipalKind.SUBUSER
        )
        writer = TupleWriter(self.authz, owner="identity")
        subject = principal_subject(principal)
        await writer.write([RelationshipTuple(subject, "member", account(self.account_id))])
        await writer.replace_subject_grants(subject, level or account(self.account_id), keys)
        return Actor(principal)

    def tuples_on(self, obj: str) -> set[str]:
        return {f"{t.relation}@{t.subject}" for t in self.authz.tuples if str(t.object) == obj}


@asynccontextmanager
async def world(
    *, store: MemoryStore | None = None, backend: Any = None, **config: Any
) -> AsyncIterator[World]:
    memory = store or MemoryStore()
    authz = InMemoryAuthorizer()
    bus = InMemoryBus()
    events = SpyEvents(memory)
    clock = Clock()
    services = build_services(
        store=memory,
        authorizer=authz,
        tuple_backend=backend(authz) if backend else authz,
        events=events,
        commands=BusCommandPublisher(bus),
        config=FleetConfig(**config),
        clock=clock,
    )
    account_id = uuid4()
    owner = Principal(subject=uuid4(), account_id=account_id, kind=PrincipalKind.USER)
    authz.add(RelationshipTuple(user(owner.subject), "owner", account(account_id)))
    yield World(services, memory, authz, bus, events, clock, Actor(owner), account_id)


async def test_fleet_register_writes_tuples_and_publishes_after_commit() -> None:
    async with world() as w:
        north = await w.services.groups.create(w.owner, w.account_id, name="North")
        found = await w.services.mowers.register(
            w.owner,
            w.account_id,
            MowerInput(name="M", connection_kind="simulated", group_ids=(north.id, north.id)),
        )
    m = found.mower
    assert w.tuples_on(f"mower:{m.id}") == {
        f"account@account:{w.account_id}",
        f"parent@site:{m.site_id}",
        f"parent@group:{north.id}",
    }
    assert w.tuples_on(f"group:{north.id}") == {f"account@account:{w.account_id}"}
    ((event_type, account_id, payload, inside_tx),) = w.events.published
    assert (event_type, account_id, inside_tx) == (MOWER_REGISTERED, w.account_id, False)
    assert isinstance(payload, MowerRegistration) and payload.mower_id == m.id
    assert found.group_ids == [north.id]


async def test_fleet_default_site_is_created_once() -> None:
    async with world() as w:
        first = await w.services.sites.ensure_default(w.account_id)
        await w.services.mowers.register(
            w.owner, w.account_id, MowerInput(name="M", connection_kind="simulated")
        )
        again = await w.services.sites.ensure_default(w.account_id)
        sites = await w.services.sites.visible(w.owner, w.account_id, include_default=True)
    assert first.id == again.id
    assert [s.id for s in sites] == [first.id]
    assert first.timezone == "Europe/Berlin" and first.is_default


async def test_fleet_openfga_down_rolls_back_and_publishes_nothing() -> None:
    async with world(backend=FlakyBackend) as w:
        backend: FlakyBackend = w.services.writer.backend  # type: ignore[assignment]
        backend.fail_writes = True
        with pytest.raises(Unavailable):
            await w.services.mowers.register(
                w.owner, w.account_id, MowerInput(name="M", connection_kind="simulated")
            )
    assert w.store.tables.mowers.rows == {}
    assert w.store.tables.sites.rows == {}
    assert w.events.published == []


async def test_fleet_failed_commit_undoes_the_tuples() -> None:
    store = FlakyStore()
    async with world(store=store) as w:
        store.fail_next_commit = True
        with pytest.raises(RuntimeError, match="commit failed"):
            await w.services.sites.create(w.owner, w.account_id, name="Home")
    assert w.store.tables.sites.rows == {}
    assert [t for t in w.authz.tuples if t.object.type == "site"] == []
    assert w.events.published == []


async def test_fleet_writer_refuses_identity_tuples() -> None:
    async with world() as w:
        with pytest.raises(TupleOwnershipError):
            await w.services.writer.write(
                [RelationshipTuple(user(uuid4()), "grant_mower_control", mower(uuid4()))]
            )


async def test_fleet_targets_need_manage_rights() -> None:
    async with world() as w:
        north = await w.services.groups.create(w.owner, w.account_id, name="North")
        operator = await w.member(["mower.view", "mower.control"])
        manager_north = await w.member(["mower.view", "fleet.manage"], group(north.id))
        with pytest.raises(ForbiddenError):
            await w.services.mowers.register(
                operator, w.account_id, MowerInput(name="M", connection_kind="simulated")
            )
        with pytest.raises(ForbiddenError):  # North is fine, but the default site needs account
            await w.services.mowers.register(
                manager_north,
                w.account_id,
                MowerInput(name="M", connection_kind="simulated", group_ids=(north.id,)),
            )
        with pytest.raises(NotFoundError):  # a foreign group is invisible
            await w.services.mowers.register(
                w.owner,
                w.account_id,
                MowerInput(name="M", connection_kind="simulated", group_ids=(uuid4(),)),
            )
        with pytest.raises(ForbiddenError):
            await w.services.groups.create(operator, w.account_id, name="South")
        with pytest.raises(NotFoundError):
            await w.services.groups.create(w.owner, w.account_id, name="X", site_id=uuid4())
        with pytest.raises(NotFoundError):  # the system actor creates nothing for principals
            await w.services.mowers.register(
                Actor(), w.account_id, MowerInput(name="M", connection_kind="simulated")
            )


async def test_fleet_move_between_groups_and_sites_rewrites_parents() -> None:
    async with world() as w:
        home = await w.services.sites.create(w.owner, w.account_id, name="Home")
        north = await w.services.groups.create(w.owner, w.account_id, name="North")
        south = await w.services.groups.create(w.owner, w.account_id, name="South")
        m = (
            await w.services.mowers.register(
                w.owner,
                w.account_id,
                MowerInput(name="M", connection_kind="simulated", group_ids=(north.id,)),
            )
        ).mower
        await w.services.mowers.set_groups(w.owner, w.account_id, m.id, [south.id])
        same = await w.services.mowers.set_groups(w.owner, w.account_id, m.id, [south.id])
        await w.services.mowers.update(w.owner, w.account_id, m.id, {"site_id": home.id})
        await w.services.groups.update(w.owner, w.account_id, north.id, {"site_id": home.id})
        with pytest.raises(NotFoundError):
            await w.services.mowers.set_groups(w.owner, w.account_id, uuid4(), [])
        with pytest.raises(ConflictError):
            await w.services.groups.update(w.owner, w.account_id, south.id, {"name": "north"})
    assert w.tuples_on(f"mower:{m.id}") == {
        f"account@account:{w.account_id}",
        f"parent@site:{home.id}",
        f"parent@group:{south.id}",
    }
    assert w.tuples_on(f"group:{north.id}") == {
        f"account@account:{w.account_id}",
        f"parent@site:{home.id}",
    }
    assert same.group_ids == [south.id]
    types = [e[0] for e in w.events.published]
    assert types == ["mower.registered", "mower.updated", "mower.updated"]  # move, site


async def test_fleet_results_only_move_records_forward() -> None:
    async with world() as w:
        m = (
            await w.services.mowers.register(
                w.owner, w.account_id, MowerInput(name="M", connection_kind="simulated")
            )
        ).mower
        accepted = await w.services.commands.send(
            w.owner,
            w.account_id,
            m.id,
            CommandRequest.model_validate({"command": {"kind": "return_to_dock"}}),
        )
        cid = accepted.command_id

        def result(status: CommandStatus, mower_id: UUID = m.id) -> CommandResult:
            return CommandResult(command_id=cid, mower_id=mower_id, status=status, reason="r")

        applied = [
            await w.services.commands.apply_result(result(CommandStatus.ACCEPTED)),
            await w.services.commands.apply_result(result(CommandStatus.ACCEPTED)),
            await w.services.commands.apply_result(result(CommandStatus.EXECUTED)),
            await w.services.commands.apply_result(result(CommandStatus.ACCEPTED)),
            await w.services.commands.apply_result(result(CommandStatus.FAILED)),
            await w.services.commands.apply_result(result(CommandStatus.EXECUTED, uuid4())),
            await w.services.commands.apply_result(
                CommandResult(command_id=uuid4(), mower_id=m.id, status=CommandStatus.EXECUTED)
            ),
        ]
        record = await w.services.commands.get(w.owner, w.account_id, cid)
        envelope = w.bus.messages(ALL_COMMANDS)[0]
    assert applied == [True, False, True, False, False, False, False]
    assert record.status == "executed"
    assert envelope.msg_id == str(cid)


async def test_fleet_out_of_order_results_keep_the_final_status() -> None:
    async with world() as w:
        m = (
            await w.services.mowers.register(
                w.owner, w.account_id, MowerInput(name="M", connection_kind="simulated")
            )
        ).mower
        request = CommandRequest.model_validate({"command": {"kind": "pause_mow"}})
        cid = (await w.services.commands.send(w.owner, w.account_id, m.id, request)).command_id
        for status in (CommandStatus.REJECTED, CommandStatus.ACCEPTED):
            await w.services.commands.apply_result(
                CommandResult(command_id=cid, mower_id=m.id, status=status, reason="busy")
            )
        record = await w.services.commands.get(w.owner, w.account_id, cid)
    assert record.status == "rejected" and record.reason == "busy"


async def test_fleet_retention_purges_old_records_only() -> None:
    async with world(command_retention=timedelta(days=90)) as w:
        old, new = uuid4(), uuid4()
        for cid, age in ((old, 91), (new, 89)):
            async with w.store.transaction() as tx:
                await tx.commands.add(
                    CommandRecord(
                        command_id=cid,
                        account_id=w.account_id,
                        mower_id=uuid4(),
                        kind="pause_mow",
                        command={"kind": "pause_mow"},
                        priority="normal",
                        issued_by_subject=uuid4(),
                        issued_by={"subject": str(uuid4()), "kind": "user"},
                        issued_at=w.clock.now - timedelta(days=age),
                        expires_at=w.clock.now - timedelta(days=age) + timedelta(seconds=30),
                        status="executed",
                        correlation_id="c",
                    )
                )
        deleted = await w.services.commands.purge()
    assert deleted == 1
    assert set(w.store.tables.commands.rows) == {new}


async def test_fleet_shard_registrations_are_disjoint_and_complete() -> None:
    async with world() as w:
        ids = {
            (
                await w.services.mowers.register(
                    w.owner, w.account_id, MowerInput(name=f"M{i}", connection_kind="simulated")
                )
            ).mower.id
            for i in range(8)
        }
        shards = [
            {r.mower_id for r in await w.services.mowers.registrations_for_shard(i, 3)}
            for i in range(3)
        ]
        everything = await w.services.mowers.registrations_for_shard(0, 1)
    assert set().union(*shards) == ids
    assert sum(len(s) for s in shards) == len(ids)
    assert all(m.int % 3 == i for i, shard in enumerate(shards) for m in shard)
    assert {r.mower_id for r in everything} == ids


async def test_fleet_event_publisher_wraps_the_payload() -> None:
    bus = InMemoryBus()
    registration = MowerRegistration(
        mower_id=uuid4(), account_id=uuid4(), name="M", connection_kind=ConnectionKind.SIMULATED
    )
    await BusEventPublisher(bus).publish(MOWER_REGISTERED, registration.account_id, registration)
    await BusEventPublisher(None).publish(MOWER_REGISTERED, registration.account_id, registration)
    await bus.close()
    await BusEventPublisher(bus).publish(MOWER_REGISTERED, registration.account_id, registration)
    (message,) = bus.messages("event.mower.registered")
    envelope = message.model(EventEnvelope[MowerRegistration])
    assert envelope.producer == "fleet" and envelope.payload == registration
    with pytest.raises(Exception, match="not connected"):
        await BusCommandPublisher(None).publish(None)  # type: ignore[arg-type]
