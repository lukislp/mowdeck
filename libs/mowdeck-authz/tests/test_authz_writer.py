"""TupleWriter: ownership, idempotency, diffs, cache invalidation, audit hook; OpenFGA backend."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from authz_fake_sdk import connected_client, factory
from mowdeck_authz import (
    AuthzSettings,
    FgaConnection,
    ObjectRef,
    OpenFgaTupleBackend,
    RelationshipTuple,
    Subject,
    TupleChange,
    TupleOwnershipError,
    TupleWriter,
    account,
    api_token,
    group,
    mower,
    not_expired,
    site,
    tuple_owner,
    user,
)
from mowdeck_authz.testing import InMemoryAuthorizer
from openfga_sdk.client.models.write_conflict_opts import ClientWriteRequestOnDuplicateWrites

LUKAS, ANNA = user("lukas"), user("anna")
A, NORTH, HOME, M1 = account("A"), group("north"), site("home"), mower("m1")


def hierarchy() -> list[RelationshipTuple]:
    return [
        RelationshipTuple(LUKAS, "owner", A),
        RelationshipTuple(A.as_subject(), "account", HOME),
        RelationshipTuple(A.as_subject(), "account", NORTH),
        RelationshipTuple(HOME.as_subject(), "parent", NORTH),
        RelationshipTuple(A.as_subject(), "account", M1),
        RelationshipTuple(NORTH.as_subject(), "parent", M1),
        RelationshipTuple(ANNA, "member", A),
    ]


class RecordingBackend:
    def __init__(self) -> None:
        self.writes: list[tuple[list[str], list[str]]] = []

    async def read(
        self, obj: object, *, subject: Subject | None = None, relation: str | None = None
    ) -> list[RelationshipTuple]:
        return []

    async def write(
        self, writes: Sequence[RelationshipTuple], deletes: Sequence[RelationshipTuple]
    ) -> None:
        self.writes.append(([str(t) for t in writes], [str(t) for t in deletes]))


class RecordingCache:
    def __init__(self) -> None:
        self.events: list[str] = []

    def invalidate(self) -> None:
        self.events.append("all")

    def invalidate_subject(self, subject: Subject | str) -> None:
        self.events.append(str(subject))


def test_tuple_ownership_table() -> None:
    assert tuple_owner("account", "owner") == "identity"
    assert tuple_owner("account", "support") == "identity"
    assert tuple_owner("mower", "grant_mower_drive") == "identity"
    assert tuple_owner("platform", "operator_admin") == "identity"
    assert tuple_owner("site", "account") == "fleet"
    assert tuple_owner("group", "parent") == "fleet"
    assert tuple_owner("mower", "account") == "fleet"
    assert tuple_owner("mower", "can_view") is None
    assert tuple_owner("account", "account") is None


async def test_ownership_guard() -> None:
    fake = InMemoryAuthorizer()
    fleet = TupleWriter(fake, owner="fleet")
    identity = TupleWriter(fake, owner="identity")
    with pytest.raises(TupleOwnershipError, match="fleet may not write"):
        await fleet.write([RelationshipTuple(LUKAS, "owner", A)])
    with pytest.raises(TupleOwnershipError, match="identity may not write"):
        await identity.write(deletes=[RelationshipTuple(A.as_subject(), "account", M1)])
    with pytest.raises(TupleOwnershipError, match="nobody"):
        await identity.write([RelationshipTuple(LUKAS, "can_view", M1)])
    assert len(fake.tuples) == 0


async def test_write_is_idempotent_and_audited() -> None:
    fake = InMemoryAuthorizer()
    audited: list[TupleChange] = []

    async def audit(change: TupleChange) -> None:
        audited.append(change)

    writer = TupleWriter(fake, audit=audit)
    change = await writer.write(hierarchy() + hierarchy())  # duplicates collapse
    assert len(change.writes) == len(hierarchy())
    await writer.write(hierarchy())  # writing again is harmless
    assert len(fake.tuples) == len(hierarchy())
    assert await fake.check(LUKAS, "can_control", M1)
    await writer.write(deletes=[RelationshipTuple(LUKAS, "owner", A)])
    await writer.write(deletes=[RelationshipTuple(LUKAS, "owner", A)])  # missing is fine
    assert not await fake.check(LUKAS, "can_control", M1)
    assert len(audited) == 4
    assert audited[0].owner is None
    assert audited[2].deletes == (RelationshipTuple(LUKAS, "owner", A),)
    assert (await writer.write()).is_empty
    assert len(audited) == 4  # empty changes are not audited


async def test_sync_audit_hook_and_rewrite_of_a_condition() -> None:
    fake = InMemoryAuthorizer(hierarchy())
    seen: list[TupleChange] = []
    backend = RecordingBackend()
    writer = TupleWriter(backend, owner="identity", audit=seen.append)
    now = datetime(2026, 10, 9, tzinfo=UTC)
    old = RelationshipTuple(user("sam"), "support", A, not_expired(now))
    new = RelationshipTuple(user("sam"), "support", A, not_expired(now + timedelta(hours=1)))
    await writer.write([new], [old])
    # Same key in writes and deletes: delete first, then write, in two transactions.
    assert backend.writes == [([], [str(old)]), ([str(new)], [])]
    assert len(seen) == 1
    fake_writer = TupleWriter(fake, owner="identity")
    await fake_writer.write([old])
    await fake_writer.write([new], [old])
    assert [t.condition for t in await fake.read(A, relation="support")] == [new.condition]


async def test_large_changes_are_chunked() -> None:
    backend = RecordingBackend()
    writer = TupleWriter(backend, max_per_write=3)
    adds = [RelationshipTuple(user(f"u{i}"), "member", A) for i in range(4)]
    deletes = [RelationshipTuple(user("old"), "member", A)]
    await writer.write(adds, deletes)
    assert [(len(w), len(d)) for w, d in backend.writes] == [(3, 0), (1, 1)]


async def test_caches_are_invalidated_per_subject_or_entirely() -> None:
    cache = RecordingCache()
    writer = TupleWriter(InMemoryAuthorizer(), caches=[cache])
    await writer.write([RelationshipTuple(ANNA, "member", A)])
    assert cache.events == ["user:anna"]
    await writer.write([RelationshipTuple(A.as_subject(), "account", M1)])
    assert cache.events == ["user:anna", "all"]


async def test_replace_subject_grants_writes_only_the_difference() -> None:
    fake = InMemoryAuthorizer(hierarchy())
    audited: list[TupleChange] = []
    writer = TupleWriter(fake, owner="identity", audit=audited.append)

    change = await writer.replace_subject_grants(
        ANNA, NORTH, ["mower.view", "mower.control", "mower.drive"]
    )
    assert {t.relation for t in change.writes} == {
        "grant_mower_view",
        "grant_mower_control",
        "grant_mower_drive",
    }
    assert await fake.check(ANNA, "can_drive", M1)

    change = await writer.replace_subject_grants(ANNA, NORTH, ["mower.view"])
    assert change.writes == ()
    assert {t.relation for t in change.deletes} == {"grant_mower_control", "grant_mower_drive"}
    assert await fake.check(ANNA, "can_view", M1)
    assert not await fake.check(ANNA, "can_control", M1)

    unchanged = await writer.replace_subject_grants(ANNA, NORTH, ["mower.view"])
    assert unchanged.is_empty
    assert len(audited) == 2

    await writer.replace_subject_grants(ANNA, A, [])  # membership is not a grant: kept
    assert RelationshipTuple(ANNA, "member", A) in fake.tuples


async def test_replace_subject_grants_validates_keys() -> None:
    writer = TupleWriter(InMemoryAuthorizer(hierarchy()), owner="identity")
    with pytest.raises(ValueError, match="cannot be granted to api_token"):
        await writer.replace_subject_grants(api_token("ha"), A, ["users.manage"])
    with pytest.raises(ValueError, match="cannot be granted on mower"):
        await writer.replace_subject_grants(ANNA, M1, ["billing.manage"])


async def test_fake_rejects_tuples_the_model_forbids() -> None:
    fake = InMemoryAuthorizer()
    with pytest.raises(ValueError, match="type restrictions"):
        fake.add(RelationshipTuple(api_token("ha"), "grant_users_manage", A))
    with pytest.raises(ValueError, match="type restrictions"):
        fake.add(RelationshipTuple(user("sam"), "support", A))  # condition required
    with pytest.raises(ValueError, match="not directly assignable"):
        fake.add(RelationshipTuple(LUKAS, "can_view", M1))
    with pytest.raises(ValueError, match="unknown type"):
        fake.add(RelationshipTuple(LUKAS, "owner", ObjectRef("camera", "c1")))
    with pytest.raises(ValueError):
        await fake.check(LUKAS, "can_fly", M1)


async def test_openfga_backend_reads_all_pages_and_writes_with_conflicts_ignored() -> None:
    client = connected_client()
    key = SimpleNamespace(user="user:anna", relation="grant_mower_view", object="group:north")
    conditioned = SimpleNamespace(
        user="user:sam",
        relation="support",
        object="account:A",
        condition=SimpleNamespace(
            name="not_expired", context={"expires_at": "2026-10-09T12:00:00Z"}
        ),
    )
    client.read_pages = [
        SimpleNamespace(tuples=[SimpleNamespace(key=key)], continuation_token="1"),  # noqa: S106
        SimpleNamespace(tuples=[SimpleNamespace(key=conditioned)], continuation_token=""),
    ]
    backend = OpenFgaTupleBackend(
        FgaConnection(AuthzSettings(api_url="http://x"), client_factory=factory(client))
    )
    found = await backend.read("group", subject=ANNA)
    assert [str(t) for t in found] == [
        "group:north#grant_mower_view@user:anna",
        "account:A#support@user:sam (with not_expired)",
    ]
    first_read = next(body for name, body in client.calls if name == "read")
    assert first_read[0].object == "group:"
    assert first_read[0].user == "user:anna"

    await backend.read(M1, relation="parent")
    reads = [body for name, body in client.calls if name == "read"]
    assert reads[-1][0].object == "mower:m1"

    sam = RelationshipTuple(
        user("sam"), "support", A, not_expired(datetime(2026, 1, 1, tzinfo=UTC))
    )
    await backend.write([sam], [RelationshipTuple(ANNA, "member", A)])
    request, options = next(body for name, body in client.calls if name == "write")
    assert request.writes[0].condition.name == "not_expired"
    assert request.deletes[0].user == "user:anna"
    assert options["conflict"].on_duplicate_writes == ClientWriteRequestOnDuplicateWrites.IGNORE
