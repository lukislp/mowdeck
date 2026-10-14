"""Integration with a real OpenFGA: Fleet writes the hierarchy tuples, OpenFGA answers.

Runs when ``MOWDECK_TEST_OPENFGA_URL`` (and ``MOWDECK_TEST_OPENFGA_TOKEN``) is set, like the
``mowdeck-authz`` integration tests (CI starts an in-memory OpenFGA 1.22). Locally::

    docker run -d --name fleet-test-fga -p 18080:8080 ghcr.io/openfga/openfga:v1.22.0 run \\
        --datastore-engine memory --authn-method preshared --authn-preshared-keys test-key
    MOWDECK_TEST_OPENFGA_URL=http://127.0.0.1:18080 MOWDECK_TEST_OPENFGA_TOKEN=test-key \\
        uv run pytest services/fleet -k openfga
"""

import os
from uuid import uuid4

import pytest
from fleet_support import API, make_settings
from mowdeck_auth.testing import TestKeys, bearer
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
    mower,
    user,
)
from mowdeck_contracts.v1 import Principal, PrincipalKind
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from fleet.adapters.memory import MemoryStore
from fleet.main import build_app

URL = os.environ.get("MOWDECK_TEST_OPENFGA_URL")
TOKEN = os.environ.get("MOWDECK_TEST_OPENFGA_TOKEN")

pytestmark = pytest.mark.skipif(not URL, reason="needs MOWDECK_TEST_OPENFGA_URL")


async def test_fleet_openfga_tuples_show_the_mower_to_its_owner_only() -> None:
    store_name = f"fleet-it-{uuid4().hex[:10]}"
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
    keys = TestKeys()
    lukas = Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER)
    bob = Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER)
    identity = TupleWriter(backend, owner="identity")
    for person in (lukas, bob):
        assert person.account_id is not None
        await identity.write(
            [
                RelationshipTuple(user(person.subject), "owner", account(person.account_id)),
                RelationshipTuple(user(person.subject), "member", account(person.account_id)),
            ]
        )
    app = build_app(
        make_settings(),
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=authz,
        tuple_backend=backend,
        principal_resolver=keys.resolver(),
        background_tasks=False,
    )
    try:
        async with lifespan_client(app) as http:
            created = await http.post(
                f"{API}/mowers",
                headers=bearer(keys.mint(lukas)),
                json={"name": "Front lawn", "connection_kind": "simulated"},
            )
            assert created.status_code == 201, created.text
            mower_id = created.json()["id"]
            owner_get = await http.get(f"{API}/mowers/{mower_id}", headers=bearer(keys.mint(lukas)))
            bob_get = await http.get(f"{API}/mowers/{mower_id}", headers=bearer(keys.mint(bob)))
            bob_stop = await http.post(
                f"{API}/mowers/{mower_id}/commands",
                headers=bearer(keys.mint(bob)),
                json={"command": {"kind": "emergency_stop"}},
            )
            tuples = await backend.read(mower(mower_id))
            answers = await authz.check_many(
                [
                    (user(lukas.subject), Relation.CAN_CONTROL, mower(mower_id)),
                    (user(bob.subject), Relation.CAN_VIEW, mower(mower_id)),
                    (user(bob.subject), Relation.CAN_EMERGENCY_STOP, mower(mower_id)),
                ]
            )
            visible_to_lukas = await authz.list_objects(
                user(lukas.subject), Relation.CAN_VIEW, "mower"
            )
            deleted = await http.delete(
                f"{API}/mowers/{mower_id}", headers=bearer(keys.mint(lukas))
            )
            after_delete = await backend.read(mower(mower_id))
    finally:
        await connection.close()
    assert owner_get.status_code == 200
    assert bob_get.status_code == 404 and bob_stop.status_code == 404
    assert {(t.relation, str(t.subject).split(":")[0]) for t in tuples} == {
        ("account", "account"),
        ("parent", "site"),
    }
    assert answers == [True, False, False]
    assert visible_to_lukas == [f"mower:{mower_id}"]
    assert deleted.status_code == 204 and after_delete == []
