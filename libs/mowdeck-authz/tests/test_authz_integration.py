"""Integration tests against a real OpenFGA server.

Skipped unless ``MOWDECK_TEST_OPENFGA_URL`` is set (CI sets it). Locally::

    docker run -d --name openfga-test -p 18080:8080 ghcr.io/openfga/openfga:v1.22.0 run \\
        --datastore-engine memory --authn-method preshared --authn-preshared-keys test-key
    MOWDECK_TEST_OPENFGA_URL=http://127.0.0.1:18080 MOWDECK_TEST_OPENFGA_TOKEN=test-key \\
        uv run pytest libs/mowdeck-authz -k integration

Every test bootstraps its own store (unique name) through ``bootstrap_model()``, so tests do not
see each other's tuples.
"""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from mowdeck_authz import (
    AuthzSettings,
    CheckItem,
    FgaConnection,
    ObjectRef,
    OpenFgaAuthorizer,
    OpenFgaTupleBackend,
    Relation,
    RelationshipTuple,
    Subject,
    TupleWriter,
    account,
    bootstrap_model,
    group,
    mower,
    not_expired,
    site,
    user,
)
from mowdeck_authz._client import create_client
from test_authz_fake_matches_model import SPEC, spec_tuples

URL = os.environ.get("MOWDECK_TEST_OPENFGA_URL")
TOKEN = os.environ.get("MOWDECK_TEST_OPENFGA_TOKEN")

pytestmark = pytest.mark.skipif(
    not URL,
    reason="OpenFGA integration tests need MOWDECK_TEST_OPENFGA_URL "
    "(e.g. docker run -p 18080:8080 ghcr.io/openfga/openfga:v1.22.0 run --datastore-engine memory)",
)

NOW = datetime(2026, 10, 9, 11, 0, tzinfo=UTC)


def settings(store_name: str, **overrides: Any) -> AuthzSettings:
    return AuthzSettings(api_url=URL or "", api_token=TOKEN, store_name=store_name, **overrides)


async def new_store() -> str:
    """Bootstrap a fresh store; a second bootstrap must be a no-op."""
    name = f"it-{uuid4().hex[:12]}"
    client = create_client(settings(name))
    try:
        first = await bootstrap_model(client, store_name=name)
        second = await bootstrap_model(client, store_name=name)
    finally:
        await client.close()
    assert first.store_created and first.model_written
    assert not second.store_created and not second.model_written
    assert (second.store_id, second.model_id) == (first.store_id, first.model_id)
    return name


@dataclass
class Phase1:
    authz: OpenFgaAuthorizer
    identity: TupleWriter
    fleet: TupleWriter
    lukas: Subject
    anna: Subject
    bob: Subject
    a: ObjectRef
    north: ObjectRef
    m1: ObjectRef
    m2: ObjectRef
    m9: ObjectRef


@pytest.fixture
async def phase1() -> AsyncIterator[Phase1]:
    """Account A of owner Lukas with sub-user Anna (Operator on group north); account B of Bob."""
    name = await new_store()
    connection = FgaConnection(settings(name))
    authz = OpenFgaAuthorizer(connection, now=lambda: NOW)
    backend = OpenFgaTupleBackend(connection)
    identity = TupleWriter(backend, owner="identity", caches=[authz])
    fleet = TupleWriter(backend, owner="fleet", caches=[authz])

    lukas, anna, bob = user(uuid4()), user(uuid4()), user(uuid4())
    a, b = account(uuid4()), account(uuid4())
    home, north, south, b_home = site(uuid4()), group(uuid4()), group(uuid4()), site(uuid4())
    m1, m2, m9 = mower(uuid4()), mower(uuid4()), mower(uuid4())

    await identity.write(
        [
            RelationshipTuple(lukas, "owner", a),
            RelationshipTuple(anna, "member", a),
            RelationshipTuple(bob, "owner", b),
        ]
    )
    await fleet.write(
        [
            RelationshipTuple(a.as_subject(), "account", home),
            RelationshipTuple(a.as_subject(), "account", north),
            RelationshipTuple(home.as_subject(), "parent", north),
            RelationshipTuple(a.as_subject(), "account", south),
            RelationshipTuple(home.as_subject(), "parent", south),
            RelationshipTuple(a.as_subject(), "account", m1),
            RelationshipTuple(home.as_subject(), "parent", m1),
            RelationshipTuple(north.as_subject(), "parent", m1),
            RelationshipTuple(a.as_subject(), "account", m2),
            RelationshipTuple(home.as_subject(), "parent", m2),
            RelationshipTuple(south.as_subject(), "parent", m2),
            RelationshipTuple(b.as_subject(), "account", b_home),
            RelationshipTuple(b.as_subject(), "account", m9),
            RelationshipTuple(b_home.as_subject(), "parent", m9),
        ]
    )
    # Identity expands the Operator role at group north into grant tuples.
    await identity.replace_subject_grants(
        anna, north, ["mower.view", "mower.control", "mower.drive"]
    )
    await authz.connect()
    yield Phase1(authz, identity, fleet, lukas, anna, bob, a, north, m1, m2, m9)
    await authz.close()


async def test_phase1_story(phase1: Phase1) -> None:
    p = phase1
    check = p.authz.check
    assert p.authz.model_id is not None
    # Owner: everything in the own account.
    for relation in (Relation.CAN_VIEW, Relation.CAN_CONTROL, Relation.CAN_DRIVE):
        assert await check(p.lukas, relation, p.m1)
        assert await check(p.lukas, relation, p.m2)
    assert await check(p.lukas, Relation.CAN_MANAGE_USERS, p.a)
    # Anna: Operator at group north only.
    assert await check(p.anna, Relation.CAN_CONTROL, p.m1)
    assert await check(p.anna, Relation.CAN_DRIVE, p.m1)
    assert await check(p.anna, Relation.CAN_EMERGENCY_STOP, p.m1)
    assert not await check(p.anna, Relation.CAN_EDIT_SCHEDULE, p.m1)
    assert not await check(p.anna, Relation.CAN_VIEW, p.m2)
    assert not await check(p.anna, Relation.CAN_BULK_COMMAND, p.north)
    assert not await check(p.anna, Relation.CAN_MANAGE_USERS, p.a)
    assert sorted(await p.authz.list_objects(p.anna, Relation.CAN_CONTROL, "mower")) == [str(p.m1)]
    assert sorted(await p.authz.list_objects(p.lukas, Relation.CAN_VIEW, "mower")) == sorted(
        [str(p.m1), str(p.m2)]
    )


async def test_cross_account_negatives(phase1: Phase1) -> None:
    p = phase1
    for relation in (
        Relation.CAN_VIEW,
        Relation.CAN_CONTROL,
        Relation.CAN_DRIVE,
        Relation.CAN_EMERGENCY_STOP,
    ):
        assert not await p.authz.check(p.bob, relation, p.m1)
        assert not await p.authz.check(p.lukas, relation, p.m9)
        assert not await p.authz.check(p.anna, relation, p.m9)
    assert not await p.authz.check(p.bob, Relation.MEMBER, p.a)
    assert not await p.authz.check(p.bob, Relation.CAN_MANAGE_USERS, p.a)
    assert await p.authz.list_objects(p.bob, Relation.CAN_VIEW, "mower") == [str(p.m9)]
    # A wrongly written cross-account grant still does not leak (tenant guard).
    await p.identity.write([RelationshipTuple(p.bob, "grant_mower_control", p.north)])
    assert not await p.authz.check(p.bob, Relation.CAN_CONTROL, p.m1)


async def test_batch_check_and_revocation(phase1: Phase1) -> None:
    p = phase1
    items = [
        (p.anna, Relation.CAN_CONTROL, p.m1),
        (p.bob, Relation.CAN_VIEW, p.m1),
        (p.lukas, Relation.CAN_DRIVE, p.m2),
        (p.anna, Relation.CAN_VIEW, p.m2),
        (p.bob, Relation.CAN_VIEW, p.m9),
    ]
    assert await p.authz.check_many(items) == [True, False, True, False, True]
    # Downgrade Anna to Viewer: the writer clears her cached decisions at once.
    change = await p.identity.replace_subject_grants(p.anna, p.north, ["mower.view"])
    assert {t.relation for t in change.deletes} == {"grant_mower_control", "grant_mower_drive"}
    assert not await p.authz.check(p.anna, Relation.CAN_CONTROL, p.m1)
    assert await p.authz.check(p.anna, Relation.CAN_VIEW, p.m1)


async def test_support_access_expires(phase1: Phase1) -> None:
    p = phase1
    sam = user(uuid4())
    expires = NOW + timedelta(hours=1)
    await p.identity.write([RelationshipTuple(sam, "support", p.a, not_expired(expires))])
    assert await p.authz.check(sam, Relation.CAN_VIEW, p.m1)  # current_time = NOW
    assert not await p.authz.check(sam, Relation.CAN_CONTROL, p.m1)
    later = {"current_time": "2026-10-09T12:00:01Z"}
    assert not await p.authz.check(sam, Relation.CAN_VIEW, p.m1, context=later)


async def test_unknown_store_fails_closed() -> None:
    authz = OpenFgaAuthorizer(settings(f"missing-{uuid4().hex[:8]}"))
    assert not await authz.ping()
    assert not await authz.check(user("x"), Relation.CAN_VIEW, mower("m1"))
    await authz.close()


@pytest.mark.parametrize("test", SPEC["tests"], ids=[t["name"] for t in SPEC["tests"]])
async def test_model_spec_through_openfga(test: dict[str, Any]) -> None:
    """The model test file, run through OpenFgaAuthorizer against the server."""
    name = await new_store()
    connection = FgaConnection(settings(name, cache_ttl_seconds=0))
    await TupleWriter(OpenFgaTupleBackend(connection)).write(spec_tuples(test))
    authz = OpenFgaAuthorizer(connection)
    failures: list[str] = []
    try:
        for case in test.get("check", []):
            subject = Subject.parse(case["user"])
            obj = ObjectRef.parse(case["object"])
            relations = list(case["assertions"])
            answers = await authz.check_many(
                [CheckItem(subject, rel, obj, case.get("context")) for rel in relations]
            )
            for relation, answer in zip(relations, answers, strict=True):
                if answer != case["assertions"][relation]:
                    failures.append(f"{subject} {relation} {obj}: {answer}")
        for case in test.get("list_objects", []):
            subject = Subject.parse(case["user"])
            for relation, expected in case["assertions"].items():
                listed = await authz.list_objects(subject, relation, case["type"])
                if sorted(listed) != sorted(expected):
                    failures.append(f"list {subject} {relation}: {listed}")
    finally:
        await authz.close()
    assert not failures, "\n".join(failures)
