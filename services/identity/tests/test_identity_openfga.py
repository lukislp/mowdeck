"""Integration with a real OpenFGA: Identity writes the tuples, OpenFGA answers the checks.

Runs when ``MOWDECK_TEST_OPENFGA_URL`` (and ``MOWDECK_TEST_OPENFGA_TOKEN``) is set, like the
``mowdeck-authz`` integration tests (CI starts an in-memory OpenFGA 1.22). Locally::

    docker run -d --name openfga-test -p 18080:8080 ghcr.io/openfga/openfga:v1.22.0 run \\
        --datastore-engine memory --authn-method preshared --authn-preshared-keys test-key
    MOWDECK_TEST_OPENFGA_URL=http://127.0.0.1:18080 MOWDECK_TEST_OPENFGA_TOKEN=test-key \\
        uv run pytest services/identity -k openfga
"""

import os
from uuid import UUID, uuid4

import pytest
from identity_support import API, PASSWORD, bearer, make_settings
from mowdeck_authz import (
    AuthzSettings,
    FgaConnection,
    OpenFgaAuthorizer,
    OpenFgaTupleBackend,
    Relation,
    RelationshipTuple,
    TupleWriter,
    account,
    bootstrap_model,
    create_client,
    group,
    mower,
    site,
    user,
)
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from identity.adapters.crypto import Argon2PasswordHasher
from identity.adapters.memory import MemoryStore
from identity.main import build_app

URL = os.environ.get("MOWDECK_TEST_OPENFGA_URL")
TOKEN = os.environ.get("MOWDECK_TEST_OPENFGA_TOKEN")

pytestmark = pytest.mark.skipif(not URL, reason="needs MOWDECK_TEST_OPENFGA_URL")


async def test_operator_on_group_north_controls_north_only_and_bob_nothing() -> None:
    store_name = f"identity-it-{uuid4().hex[:10]}"
    settings = AuthzSettings(
        api_url=URL or "", api_token=TOKEN, store_name=store_name, cache_ttl_seconds=0
    )
    client = create_client(settings)
    try:
        await bootstrap_model(client, store_name=store_name)
    finally:
        await client.close()
    connection = FgaConnection(settings)
    authz = OpenFgaAuthorizer(connection)
    backend = OpenFgaTupleBackend(connection)
    app = build_app(
        make_settings(),
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=authz,
        tuple_backend=backend,
        hasher=Argon2PasswordHasher("insecure_test"),
        background_tasks=False,
    )
    try:
        async with lifespan_client(app) as http:

            async def register(name: str) -> tuple[UUID, str]:
                created = await http.post(
                    f"{API}/register",
                    json={"username": name, "password": PASSWORD, "account_name": f"{name} home"},
                )
                assert created.status_code == 201, created.text
                login = await http.post(
                    f"{API}/session", json={"username": name, "password": PASSWORD}
                )
                return UUID(created.json()["account_id"]), login.json()["access_token"]

            account_a, lukas = await register("lukas")
            account_b, bob = await register("bob")
            home, north, south, m1, m2 = (uuid4() for _ in range(5))
            fleet = TupleWriter(backend, owner="fleet")
            acc = account(account_a).as_subject()
            await fleet.write(
                [
                    RelationshipTuple(acc, "account", site(home)),
                    RelationshipTuple(acc, "account", group(north)),
                    RelationshipTuple(acc, "account", group(south)),
                    RelationshipTuple(site(home).as_subject(), "parent", group(north)),
                    RelationshipTuple(site(home).as_subject(), "parent", group(south)),
                    RelationshipTuple(acc, "account", mower(m1)),
                    RelationshipTuple(group(north).as_subject(), "parent", mower(m1)),
                    RelationshipTuple(acc, "account", mower(m2)),
                    RelationshipTuple(group(south).as_subject(), "parent", mower(m2)),
                ]
            )
            roles = (
                await http.get(f"{API}/accounts/{account_a}/roles", headers=bearer(lukas))
            ).json()
            operator = next(r["id"] for r in roles if r["key"] == "operator")
            created = await http.post(
                f"{API}/accounts/{account_a}/users",
                headers=bearer(lukas),
                json={
                    "username": "anna",
                    "password": PASSWORD,
                    "assignments": [
                        {"role_id": operator, "level": {"type": "group", "id": str(north)}}
                    ],
                },
            )
            assert created.status_code == 201, created.text
            anna = user(created.json()["id"])
            bob_user = user((await http.get(f"{API}/me", headers=bearer(bob))).json()["subject"])
            foreign_grant = await http.post(
                f"{API}/accounts/{account_b}/role-assignments",
                headers=bearer(bob),
                json={
                    "subject_id": str(bob_user.id),
                    "role_id": next(
                        r["id"]
                        for r in (
                            await http.get(f"{API}/accounts/{account_b}/roles", headers=bearer(bob))
                        ).json()
                        if r["key"] == "operator"
                    ),
                    "level": {"type": "group", "id": str(north)},
                },
            )
            answers = await authz.check_many(
                [
                    (anna, Relation.CAN_CONTROL, mower(m1)),
                    (anna, Relation.CAN_CONTROL, mower(m2)),
                    (anna, Relation.CAN_EMERGENCY_STOP, mower(m1)),
                    (anna, Relation.CAN_MANAGE_USERS, account(account_a)),
                    (bob_user, Relation.CAN_VIEW, mower(m1)),
                    (bob_user, Relation.CAN_CONTROL, mower(m1)),
                    (bob_user, Relation.CAN_EMERGENCY_STOP, mower(m2)),
                ]
            )
            anna_mowers = await authz.list_objects(anna, Relation.CAN_CONTROL, "mower")
            disabled = await http.post(
                f"{API}/accounts/{account_a}/users/{anna.id}/disable", headers=bearer(lukas)
            )
            after_disable = await authz.check(anna, Relation.CAN_CONTROL, mower(m1))
    finally:
        await connection.close()
    assert answers == [True, False, True, False, False, False, False]
    assert anna_mowers == [f"mower:{m1}"]
    assert foreign_grant.status_code == 404  # north belongs to account A
    assert disabled.status_code == 200
    assert after_disable is False
