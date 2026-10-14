"""Integration with a real OpenFGA: the packaged model decides who sees a mower's telemetry.

Runs when ``MOWDECK_TEST_OPENFGA_URL`` (and ``MOWDECK_TEST_OPENFGA_TOKEN``) is set, like the
``mowdeck-authz`` integration tests (CI starts an in-memory OpenFGA 1.22). Locally::

    docker run -d --name telemetry-test-fga -p 18080:8080 ghcr.io/openfga/openfga:v1.22.0 run \\
        --datastore-engine memory --authn-method preshared --authn-preshared-keys test-key
    MOWDECK_TEST_OPENFGA_URL=http://127.0.0.1:18080 MOWDECK_TEST_OPENFGA_TOKEN=test-key \\
        uv run pytest services/telemetry -k openfga
"""

import os
from uuid import uuid4

import pytest
from mowdeck_auth.testing import TestKeys, bearer
from mowdeck_authz import (
    AuthzSettings,
    FgaConnection,
    OpenFgaAuthorizer,
    OpenFgaTupleBackend,
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
from telemetry_support import API, make_settings, make_state

from telemetry.adapters.memory import MemoryStore
from telemetry.domain.models import LatestRow
from telemetry.main import build_app

URL = os.environ.get("MOWDECK_TEST_OPENFGA_URL")
TOKEN = os.environ.get("MOWDECK_TEST_OPENFGA_TOKEN")

pytestmark = pytest.mark.skipif(not URL, reason="needs MOWDECK_TEST_OPENFGA_URL")


async def test_telemetry_openfga_a_visible_mower_is_200_and_a_foreign_one_404() -> None:
    store_name = f"telemetry-it-{uuid4().hex[:10]}"
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
    fleet = TupleWriter(backend, owner="fleet")
    mower_id = uuid4()
    for person in (lukas, bob):
        assert person.account_id is not None
        await identity.write(
            [
                RelationshipTuple(user(person.subject), "owner", account(person.account_id)),
                RelationshipTuple(user(person.subject), "member", account(person.account_id)),
            ]
        )
    assert lukas.account_id is not None
    await fleet.write(
        [RelationshipTuple(account(lukas.account_id).as_subject(), "account", mower(mower_id))]
    )
    store = MemoryStore()
    app = build_app(
        make_settings(),
        bus=InMemoryBus(),
        store=store,
        authorizer=authz,
        principal_resolver=keys.resolver(),
        background_tasks=False,
    )
    try:
        async with lifespan_client(app) as http:
            async with store.transaction() as tx:
                state = make_state(mower_id, lukas.account_id)
                await tx.put_latest(
                    LatestRow(
                        mower_id=mower_id,
                        account_id=lukas.account_id,
                        observed_at=state.observed_at,
                        received_at=state.observed_at,
                        activity=state.activity.value,
                        battery_percent=state.battery_percent,
                        state=state.model_dump(mode="json"),
                    )
                )
            path = f"{API}/mowers/{mower_id}/state"
            owner_get = await http.get(path, headers=bearer(keys.mint(lukas)))
            bob_get = await http.get(path, headers=bearer(keys.mint(bob)))
            listed = await http.get(
                f"{API}/accounts/me/mowers/states", headers=bearer(keys.mint(lukas))
            )
            bob_listed = await http.get(
                f"{API}/accounts/me/mowers/states", headers=bearer(keys.mint(bob))
            )
    finally:
        await connection.close()
    assert owner_get.status_code == 200 and owner_get.json()["state"]["activity"] == "docked"
    assert bob_get.status_code == 404
    assert [i["mower_id"] for i in listed.json()["items"]] == [str(mower_id)]
    assert bob_listed.json()["items"] == []
