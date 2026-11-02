"""Helpers for the identity tests (a plain module: the repository has no conftest.py).

``harness()`` builds the real app with the memory store, the in-memory OpenFGA fake (which
evaluates the packaged model), the in-memory bus and a fast Argon2id profile, and yields a
:class:`Harness` with an HTTP client and shortcuts for the common flows.
"""

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from mowdeck_authz import RelationshipTuple, TupleWriter, account, group, mower, site
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import ModuleDescriptor, PermissionDef
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from identity.adapters.crypto import Argon2PasswordHasher
from identity.adapters.memory import MemoryStore
from identity.application.ports import FetchedDescriptor
from identity.main import build_app
from identity.settings import Settings

TEST_KEK = base64.urlsafe_b64encode(b"k" * 32).decode()
"""Test only key-encryption key."""

PASSWORD = "correct horse battery"  # noqa: S105 - test password
API = "/api/v1/identity"


class MutableClock:
    """A wall clock that tests can move forward."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, delta: timedelta) -> None:
        """Move the clock forward."""
        self.offset += delta


class FakeModules:
    """A module source answering with fixed descriptors."""

    def __init__(self, *descriptors: ModuleDescriptor) -> None:
        self.descriptors = list(descriptors)
        self.down: set[str] = set()

    async def fetch(self) -> list[FetchedDescriptor]:
        return [
            FetchedDescriptor(d.name, None if d.name in self.down else d) for d in self.descriptors
        ]


GATEWAY = ModuleDescriptor(
    name="device-gateway",
    version="0.1.0",
    permissions=[
        PermissionDef(
            key="device_gateway.view",
            description="View the mowers held by the Device-Gateway",
            implied_by=["mower.view"],
        )
    ],
)


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests (environment independent)."""
    values: dict[str, Any] = {
        "environment": "test",
        "log_format": "console",
        "nats_enabled": False,
        "identity_key_encryption_key": TEST_KEK,
        "identity_refresh_cookie_secure": False,
        "identity_login_ip_burst": 1000,
        "identity_login_ip_per_minute": 1000,
        **overrides,
    }
    return Settings.model_validate(values)


def bearer(token: str) -> dict[str, str]:
    """Authorization header."""
    return {"Authorization": f"Bearer {token}"}


@dataclass
class Account:
    """A registered account and its owner's tokens."""

    account_id: UUID
    user_id: UUID
    username: str
    token: str
    refresh_token: str

    @property
    def headers(self) -> dict[str, str]:
        """Bearer header of the owner."""
        return bearer(self.token)

    def url(self, path: str = "") -> str:
        """``/api/v1/identity/accounts/<id><path>``."""
        return f"{API}/accounts/{self.account_id}{path}"


@dataclass
class Harness:
    """The app under test and its fakes."""

    client: httpx.AsyncClient
    store: MemoryStore
    authz: InMemoryAuthorizer
    bus: InMemoryBus
    clock: MutableClock
    modules: FakeModules
    app: Any
    fleet: TupleWriter = field(init=False)

    def __post_init__(self) -> None:
        self.fleet = TupleWriter(self.authz, owner="fleet")

    async def register(self, username: str, *, account_name: str | None = None) -> Account:
        """Register and log in."""
        response = await self.client.post(
            f"{API}/register",
            json={
                "username": username,
                "password": PASSWORD,
                "account_name": account_name or f"{username} home",
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        login = await self.login(username)
        return Account(
            UUID(body["account_id"]),
            UUID(body["user_id"]),
            username,
            login["access_token"],
            login["refresh_token"],
        )

    async def login(self, username: str, password: str = PASSWORD, **extra: Any) -> dict[str, Any]:
        """Log in; asserts success."""
        response = await self.client.post(
            f"{API}/session", json={"username": username, "password": password, **extra}
        )
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result

    async def role_id(self, owner: Account, key: str) -> UUID:
        """Id of a role by key."""
        roles = (await self.client.get(owner.url("/roles"), headers=owner.headers)).json()
        return UUID(next(r["id"] for r in roles if r["key"] == key))

    async def subuser(
        self, owner: Account, username: str, assignments: list[dict[str, Any]] | None = None
    ) -> tuple[UUID, str]:
        """Create a sub-user and log it in; returns ``(user_id, access_token)``."""
        response = await self.client.post(
            owner.url("/users"),
            headers=owner.headers,
            json={"username": username, "password": PASSWORD, "assignments": assignments or []},
        )
        assert response.status_code == 201, response.text
        user_id = UUID(response.json()["id"])
        return user_id, (await self.login(username))["access_token"]

    async def place(self, account_id: UUID) -> dict[str, UUID]:
        """Fleet's hierarchy: site home with groups north and south, mower m1 in north, m2 in
        south (written as Fleet would)."""
        ids = {"home": uuid4(), "north": uuid4(), "south": uuid4(), "m1": uuid4(), "m2": uuid4()}
        acc = account(account_id).as_subject()
        home = site(ids["home"])
        await self.fleet.write(
            [
                RelationshipTuple(acc, "account", home),
                RelationshipTuple(acc, "account", group(ids["north"])),
                RelationshipTuple(home.as_subject(), "parent", group(ids["north"])),
                RelationshipTuple(acc, "account", group(ids["south"])),
                RelationshipTuple(home.as_subject(), "parent", group(ids["south"])),
                RelationshipTuple(acc, "account", mower(ids["m1"])),
                RelationshipTuple(home.as_subject(), "parent", mower(ids["m1"])),
                RelationshipTuple(group(ids["north"]).as_subject(), "parent", mower(ids["m1"])),
                RelationshipTuple(acc, "account", mower(ids["m2"])),
                RelationshipTuple(home.as_subject(), "parent", mower(ids["m2"])),
                RelationshipTuple(group(ids["south"]).as_subject(), "parent", mower(ids["m2"])),
            ]
        )
        return ids

    def tuples_of(self, subject: str) -> set[str]:
        """``object#relation`` of every tuple of ``subject`` in the fake."""
        return {f"{t.object}#{t.relation}" for t in self.authz.tuples if str(t.subject) == subject}

    async def audit(self, owner: Account, **params: Any) -> list[dict[str, Any]]:
        """The owner's audit log."""
        response = await self.client.get(owner.url("/audit"), headers=owner.headers, params=params)
        assert response.status_code == 200, response.text
        result: list[dict[str, Any]] = response.json()
        return result


@asynccontextmanager
async def harness(
    *,
    modules: FakeModules | None = None,
    store: Any = None,
    backend: Any = None,
    authz: InMemoryAuthorizer | None = None,
    entitlements: Any = None,
    github_transport: httpx.AsyncBaseTransport | None = None,
    apple_transport: httpx.AsyncBaseTransport | None = None,
    provisioner: Any = None,
    **overrides: Any,
) -> AsyncIterator[Harness]:
    """The app with fakes, inside its lifespan. ``backend`` replaces the tuple backend;
    ``github_transport`` answers for GitHub (``github_support.FakeGitHub``), ``apple_transport``
    for Apple (``apple_support.FakeApple``)."""
    memory = store or MemoryStore()
    authz = authz or InMemoryAuthorizer()
    bus = InMemoryBus()
    clock = MutableClock()
    fake_modules = modules or FakeModules(GATEWAY)
    app = build_app(
        make_settings(**overrides),
        bus=bus,
        store=memory,
        authorizer=authz,
        tuple_backend=backend or authz,
        module_source=fake_modules,
        hasher=Argon2PasswordHasher("insecure_test"),
        clock=clock,
        background_tasks=False,
        entitlements=entitlements,
        github_transport=github_transport,
        apple_transport=apple_transport,
        provisioner=provisioner,
    )
    async with lifespan_client(app) as client:
        await app.state.identity.deps.catalogue.refresh(fetch_modules=True)
        yield Harness(client, memory, authz, bus, clock, fake_modules, app)


class FlakyBackend:
    """A tuple backend over the fake that fails on demand (OpenFGA outage)."""

    def __init__(self, inner: InMemoryAuthorizer) -> None:
        self.inner = inner
        self.fail_writes = False
        self.fail_reads = False

    async def read(self, obj: Any, *, subject: Any = None, relation: Any = None) -> Any:
        if self.fail_reads:
            raise ConnectionError("openfga down")
        return await self.inner.read(obj, subject=subject, relation=relation)

    async def write(self, writes: Any, deletes: Any) -> None:
        if self.fail_writes:
            raise ConnectionError("openfga down")
        await self.inner.write(writes, deletes)


class FlakyStore(MemoryStore):
    """A memory store whose next commit fails (after the OpenFGA write)."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_commit = False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        async with super().transaction() as tx:
            yield tx
            if self.fail_next_commit:
                self.fail_next_commit = False
                raise RuntimeError("commit failed")
