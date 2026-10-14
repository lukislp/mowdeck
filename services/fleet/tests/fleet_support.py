"""Helpers for the fleet tests (a plain module: the repository has no conftest.py).

``harness()`` builds the real app with the memory store, the in-memory OpenFGA fake (it
evaluates the packaged model), the in-memory bus and access tokens minted with
``mowdeck_auth.testing.TestKeys``, and yields a :class:`Harness` with an HTTP client and
shortcuts. Identity's part (owners, members, role grants) is written into the fake with
``TupleWriter(owner="identity")``, the way Identity writes it.
"""

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from mowdeck_auth.testing import TestKeys, bearer
from mowdeck_authz import (
    ObjectRef,
    RelationshipTuple,
    TupleWriter,
    account,
    principal_subject,
)
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    Principal,
    PrincipalKind,
)
from mowdeck_contracts.v1.subjects import ALL_COMMANDS
from mowdeck_service_kit.bus import BusMessage, InMemoryBus, publish_command_result
from mowdeck_service_kit.testing import lifespan_client

from fleet.adapters.memory import MemoryStore
from fleet.main import build_app
from fleet.settings import Settings

API = "/api/v1/fleet"

ROLE_KEYS: dict[str, tuple[str, ...]] = {
    "manager": (
        "mower.view",
        "mower.control",
        "schedule.edit",
        "fleet.bulk_command",
        "fleet.manage",
    ),
    "operator": ("mower.view", "mower.control", "mower.drive"),
    "viewer": ("mower.view",),
}
"""Identity's default roles (``identity.domain.permissions``)."""


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests (environment independent)."""
    values: dict[str, Any] = {
        "environment": "test",
        "log_format": "console",
        "nats_enabled": False,
        **overrides,
    }
    return Settings.model_validate(values)


@dataclass
class Person:
    """A principal of an account with its access token."""

    principal: Principal
    token: str

    @property
    def headers(self) -> dict[str, str]:
        """Bearer header."""
        return bearer(self.token)

    @property
    def account_id(self) -> UUID:
        """The account of the token."""
        assert self.principal.account_id is not None
        return self.principal.account_id


@dataclass
class Harness:
    """The app under test and its fakes."""

    client: httpx.AsyncClient
    store: MemoryStore
    authz: InMemoryAuthorizer
    bus: InMemoryBus
    keys: TestKeys
    app: FastAPI
    backend: Any = None
    identity: TupleWriter = field(init=False)

    def __post_init__(self) -> None:
        self.identity = TupleWriter(self.authz, owner="identity")

    def person(
        self,
        account_id: UUID,
        *,
        kind: PrincipalKind = PrincipalKind.USER,
        permissions: Iterable[str] = (),
    ) -> Person:
        """A principal with a minted token (no tuples)."""
        principal = Principal(
            subject=uuid4(), account_id=account_id, kind=kind, permissions=list(permissions)
        )
        return Person(principal, self.keys.mint(principal))

    async def owner(self) -> Person:
        """A new account and its owner (``account#owner``, ``account#member``)."""
        person = self.person(
            uuid4(), permissions=sorted({k for v in ROLE_KEYS.values() for k in v})
        )
        subject = principal_subject(person.principal)
        await self.identity.write(
            [
                RelationshipTuple(subject, "owner", account(person.account_id)),
                RelationshipTuple(subject, "member", account(person.account_id)),
            ]
        )
        return person

    async def member(self, owner: Person, role: str, level: ObjectRef | None = None) -> Person:
        """A sub-user of ``owner``'s account with ``role`` at ``level`` (default: the account)."""
        person = self.person(
            owner.account_id, kind=PrincipalKind.SUBUSER, permissions=ROLE_KEYS[role]
        )
        subject = principal_subject(person.principal)
        await self.identity.write([RelationshipTuple(subject, "member", account(owner.account_id))])
        await self.identity.replace_subject_grants(
            subject, level or account(owner.account_id), ROLE_KEYS[role]
        )
        return person

    async def register(self, owner: Person, **body: Any) -> dict[str, Any]:
        """Register a simulated mower; asserts 201."""
        payload = {"name": f"mower-{uuid4().hex[:6]}", "connection_kind": "simulated", **body}
        response = await self.client.post(f"{API}/mowers", headers=owner.headers, json=payload)
        assert response.status_code == 201, response.text
        result: dict[str, Any] = response.json()
        return result

    async def create(self, owner: Person, path: str, **body: Any) -> dict[str, Any]:
        """``POST /api/v1/fleet/<path>``; asserts 201."""
        response = await self.client.post(f"{API}/{path}", headers=owner.headers, json=body)
        assert response.status_code == 201, response.text
        result: dict[str, Any] = response.json()
        return result

    def tuples_on(self, obj: str) -> set[str]:
        """``relation@subject`` of every tuple on ``obj`` in the fake."""
        return {f"{t.relation}@{t.subject}" for t in self.authz.tuples if str(t.object) == obj}

    def commands_published(self) -> list[CommandEnvelope]:
        """Every command envelope on ``command.mower.*``."""
        return [m.model(CommandEnvelope) for m in self.bus.messages(ALL_COMMANDS)]


async def fake_gateway(
    bus: InMemoryBus,
    statuses: Iterable[CommandStatus] = (CommandStatus.ACCEPTED, CommandStatus.EXECUTED),
) -> None:
    """Answer every command like the Device-Gateway: publish ``statuses`` in order."""
    answers = list(statuses)

    async def handle(message: BusMessage) -> None:
        envelope = message.model(CommandEnvelope)
        for status in answers:
            await publish_command_result(
                bus,
                CommandResult(
                    command_id=envelope.command_id,
                    mower_id=envelope.mower_id,
                    status=status,
                    correlation_id=envelope.correlation_id,
                ),
            )

    await bus.subscribe(ALL_COMMANDS, handle, durable="device-gateway-0")


class FlakyBackend:
    """A tuple backend over the fake that fails on demand (OpenFGA outage)."""

    def __init__(self, inner: InMemoryAuthorizer) -> None:
        self.inner = inner
        self.fail_writes = False

    async def read(self, obj: Any, *, subject: Any = None, relation: Any = None) -> Any:
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


@asynccontextmanager
async def harness(
    *,
    store: Any = None,
    backend: Callable[[InMemoryAuthorizer], Any] | None = None,
    bus: Any = None,
    entitlements: Any = None,
    clock: Any = None,
    **overrides: Any,
) -> AsyncIterator[Harness]:
    """The app with fakes, inside its lifespan. ``backend(authz)`` replaces the tuple
    backend (e.g. :class:`FlakyBackend`); ``entitlements`` replaces the plan client (e.g.
    ``FakeEntitlements``)."""
    memory = store or MemoryStore()
    authz = InMemoryAuthorizer()
    tuple_backend = backend(authz) if backend is not None else authz
    active_bus = bus or InMemoryBus()
    keys = TestKeys()
    app = build_app(
        make_settings(**overrides),
        bus=active_bus,
        store=memory,
        authorizer=authz,
        tuple_backend=tuple_backend,
        principal_resolver=keys.resolver(),
        background_tasks=False,
        entitlements=entitlements,
        clock=clock,
    )
    async with lifespan_client(app) as client:
        yield Harness(client, memory, authz, active_bus, keys, app, tuple_backend)
