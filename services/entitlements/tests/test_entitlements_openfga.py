"""Integration with a real OpenFGA: ``platform:main#can_manage_plans`` gates the operator API.

Runs when ``MOWDECK_TEST_OPENFGA_URL`` (and ``MOWDECK_TEST_OPENFGA_TOKEN``) is set, like the
``mowdeck-authz`` integration tests (CI starts an in-memory OpenFGA 1.22). Locally::

    docker run -d --name entitlements-test-fga -p 18080:8080 ghcr.io/openfga/openfga:v1.22.0 \\
        run --datastore-engine memory --authn-method preshared --authn-preshared-keys test-key
    MOWDECK_TEST_OPENFGA_URL=http://127.0.0.1:18080 MOWDECK_TEST_OPENFGA_TOKEN=test-key \\
        uv run pytest services/entitlements -k openfga
"""

import os
from uuid import uuid4

import pytest
from entitlements_support import API, make_settings, repository_plans
from mowdeck_auth.testing import TestKeys, bearer
from mowdeck_authz import (
    PLATFORM_OBJECT,
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
    user,
)
from mowdeck_contracts.v1 import Principal, PrincipalKind
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from entitlements.adapters.memory import MemoryStore
from entitlements.main import build_app

URL = os.environ.get("MOWDECK_TEST_OPENFGA_URL")
TOKEN = os.environ.get("MOWDECK_TEST_OPENFGA_TOKEN")

pytestmark = pytest.mark.skipif(not URL, reason="needs MOWDECK_TEST_OPENFGA_URL")


async def test_entitlements_openfga_only_operator_admins_manage_plans() -> None:
    store_name = f"entitlements-it-{uuid4().hex[:10]}"
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
    identity = TupleWriter(OpenFgaTupleBackend(connection), owner="identity")
    keys = TestKeys()
    owner = Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER)
    admin = Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER)
    assert owner.account_id is not None and admin.account_id is not None
    await identity.write(
        [
            RelationshipTuple(user(owner.subject), "owner", account(owner.account_id)),
            RelationshipTuple(user(admin.subject), "owner", account(admin.account_id)),
            RelationshipTuple(user(admin.subject), "operator_admin", PLATFORM_OBJECT),
        ]
    )
    try:
        assert await authz.check(user(admin.subject), Relation.CAN_MANAGE_PLANS, PLATFORM_OBJECT)
        assert await authz.check(user(admin.subject), Relation.CAN_MANAGE_ACCOUNTS, PLATFORM_OBJECT)
        assert not await authz.check(
            user(owner.subject), Relation.CAN_MANAGE_PLANS, PLATFORM_OBJECT
        )
        app = build_app(
            make_settings(entitlements_billing_enabled=True),
            bus=InMemoryBus(),
            store=MemoryStore(),
            authorizer=authz,
            principal_resolver=keys.resolver(),
            plan_specs=repository_plans(),
            background_tasks=False,
        )
        async with lifespan_client(app) as http:
            path = f"{API}/accounts/{owner.account_id}/plan"
            body = {"plan": "fleet", "reason": "openfga"}
            denied = await http.put(path, headers=bearer(keys.mint(owner)), json=body)
            assert denied.status_code == 403
            allowed = await http.put(path, headers=bearer(keys.mint(admin)), json=body)
            assert allowed.status_code == 200, allowed.text
            assert allowed.json()["effective"]["plan"] == "fleet"
            listing = await http.get(f"{API}/accounts", headers=bearer(keys.mint(admin)))
            assert listing.status_code == 200
            assert (await http.get("/readyz")).status_code == 200
    finally:
        await connection.close()
