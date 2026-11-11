# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""The short-lived deny list (ADR 0082): a token of a deleted user or an erased account is
refused although its signature and expiry are fine. The list is pure (the time is injected), the
verifier asks it, the bus feeds it, a restart reads it back, and without a bus nothing changes.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from mowdeck_auth import (
    AuthSettings,
    JwtPrincipalResolver,
    JwtVerifier,
    RevocationList,
    TokenError,
    subscribe_revocations,
)
from mowdeck_auth.settings import MAX_LEEWAY_SECONDS
from mowdeck_auth.testing import TestKeys, bearer
from mowdeck_contracts.v1 import (
    IDENTITY_ACCESS_REVOKED,
    AccessRevoked,
    AccessScope,
    EventEnvelope,
    Principal,
    PrincipalKind,
)
from mowdeck_contracts.v1.subjects import ALL_ACCESS_REVOKED, access_revoked_subject
from mowdeck_service_kit import create_app, current_principal
from mowdeck_service_kit.bus import InMemoryBus, publish_event
from mowdeck_service_kit.tenancy import BusAware
from mowdeck_service_kit.testing import lifespan_client, make_test_descriptor, make_test_settings

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
LIFETIME = timedelta(minutes=11)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def user(account_id: UUID | None = None, subject: UUID | None = None) -> Principal:
    return Principal(
        subject=subject or uuid4(),
        account_id=account_id or uuid4(),
        kind=PrincipalKind.USER,
        permissions=["notifications.manage"],
    )


def revoked(scope: AccessScope, target: UUID, at: datetime = T0) -> AccessRevoked:
    return AccessRevoked(scope=scope, id=target, issued_before=at, expires_at=at + LIFETIME)


async def publish(bus: InMemoryBus, entry: AccessRevoked) -> None:
    """What Identity's publisher sends."""
    envelope = EventEnvelope[AccessRevoked](
        type=IDENTITY_ACCESS_REVOKED, producer="identity", payload=entry
    )
    await publish_event(bus, envelope, subject=access_revoked_subject(entry.id))


# --- the list --------------------------------------------------------------------------------


def test_a_token_issued_before_the_revocation_is_refused_until_the_entry_expires() -> None:
    clock = Clock()
    entries = RevocationList(clock)
    anna, bob = user(), user()
    assert entries.add(revoked(AccessScope.SUBJECT, anna.subject))

    assert entries.refuses(anna, T0 - timedelta(minutes=3))
    assert entries.refuses(anna, T0)  # issued in the moment of the deletion
    assert not entries.refuses(bob, T0 - timedelta(minutes=3))
    assert not entries.refuses(anna, T0 + timedelta(seconds=1))  # a later sign-in is not meant

    clock.advance(minutes=11)
    assert not entries.refuses(anna, T0 - timedelta(minutes=3))  # that token expired by itself
    assert len(entries) == 0


def test_an_account_entry_covers_every_token_of_the_account_and_no_other() -> None:
    entries = RevocationList(Clock())
    account_id = uuid4()
    owner, token, stranger = user(account_id), user(account_id), user()
    operator = Principal(subject=uuid4(), kind=PrincipalKind.SERVICE)
    entries.add(revoked(AccessScope.ACCOUNT, account_id))

    assert entries.refuses(owner, T0) and entries.refuses(token, T0 - timedelta(minutes=9))
    assert not entries.refuses(stranger, T0)
    assert not entries.refuses(operator, T0)  # bound to no account
    # A subject id that happens to equal the account id is not the account.
    assert not entries.refuses(user(subject=account_id), T0)


def test_an_expired_entry_is_not_kept_and_a_repeated_one_keeps_the_later_limits() -> None:
    clock = Clock()
    entries = RevocationList(clock)
    anna = user()
    assert not entries.add(revoked(AccessScope.SUBJECT, anna.subject, T0 - timedelta(minutes=11)))
    assert len(entries) == 0

    entries.add(revoked(AccessScope.SUBJECT, anna.subject, T0))
    entries.add(revoked(AccessScope.SUBJECT, anna.subject, T0 - timedelta(minutes=5)))  # older
    assert entries.refuses(anna, T0)
    assert len(entries) == 1


def test_the_list_is_bounded() -> None:
    entries = RevocationList(Clock(), max_entries=3)
    ids = [uuid4() for _ in range(5)]
    for index, subject in enumerate(ids):
        entries.add(revoked(AccessScope.SUBJECT, subject, T0 + timedelta(seconds=index)))
    assert len(entries) == 3
    assert not entries.refuses(user(subject=ids[0]), T0)  # the oldest went first
    assert entries.refuses(user(subject=ids[4]), T0)


# --- the verifier ----------------------------------------------------------------------------


async def test_the_verifier_refuses_a_revoked_token_and_accepts_the_others() -> None:
    keys = TestKeys()  # the wall clock: the signature check compares ``exp`` with it
    verifier = keys.verifier()
    anna, bob = user(), user()
    old, other = keys.mint(anna), keys.mint(bob)
    assert (await verifier.verify(old)).subject == anna.subject

    verifier.revocations.add(revoked(AccessScope.SUBJECT, anna.subject, datetime.now(UTC)))

    with pytest.raises(TokenError, match="access revoked"):
        await verifier.verify(old)
    assert (await verifier.verify(other)).subject == bob.subject


# --- the bus ---------------------------------------------------------------------------------


async def test_every_process_learns_a_revocation_and_a_restarted_one_reads_it_back() -> None:
    bus = InMemoryBus()
    first, second = RevocationList(lambda: T0), RevocationList(lambda: T0)
    await subscribe_revocations(bus, first)
    await subscribe_revocations(bus, second)
    anna = user()

    await publish(bus, revoked(AccessScope.SUBJECT, anna.subject))
    await publish(bus, revoked(AccessScope.ACCOUNT, anna.account_id or uuid4()))

    assert first.refuses(anna, T0) and second.refuses(anna, T0)  # no queue group: both replicas
    assert len(first) == 2

    restarted = RevocationList(lambda: T0)  # a process that starts while the entry is in force
    await subscribe_revocations(bus, restarted)
    assert restarted.refuses(anna, T0)

    late = RevocationList(lambda: T0 + timedelta(minutes=12))  # ... and one that starts later
    await subscribe_revocations(bus, late)
    assert len(late) == 0


async def test_a_malformed_entry_is_dropped_and_changes_nothing() -> None:
    bus = InMemoryBus()
    entries = RevocationList(lambda: T0)
    await subscribe_revocations(bus, entries)

    await bus.publish(access_revoked_subject(uuid4()), b'{"type": "identity.access.revoked"}')

    assert len(entries) == 0
    assert len(bus.dropped) == 1
    assert ALL_ACCESS_REVOKED == "event.identity.access.revoked.*"


# --- through the kit: no code in a service ---------------------------------------------------


def _app(keys: TestKeys, bus: InMemoryBus | None) -> FastAPI:
    app = create_app(
        make_test_settings(nats_enabled=False),
        make_test_descriptor(),
        bus=bus,
        principal_resolver=keys.resolver(),
    )

    @app.put("/preferences")
    async def write(principal: Principal = Depends(current_principal)) -> dict[str, str]:  # noqa: B008
        return {"written_by": str(principal.subject)}

    return app


async def test_a_service_refuses_the_token_of_a_deleted_user_without_any_code_of_its_own() -> None:
    keys, bus = TestKeys(), InMemoryBus()
    anna, bob = user(), user()
    token, other = keys.mint(anna), keys.mint(bob)
    assert isinstance(keys.resolver(), BusAware)
    async with lifespan_client(_app(keys, bus)) as client:
        before = await client.put("/preferences", headers=bearer(token))

        now = datetime.now(UTC)
        await publish(bus, revoked(AccessScope.SUBJECT, anna.subject, now))

        after = await client.put("/preferences", headers=bearer(token))
        still = await client.put("/preferences", headers=bearer(other))
    assert before.status_code == 200
    assert after.status_code == 401
    assert after.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    assert "revoked" not in after.text  # the reason is for the log only
    assert still.status_code == 200


async def test_without_a_bus_tokens_are_accepted_as_before() -> None:
    """Fail open: a service that cannot hear revocations locks nobody out."""
    keys = TestKeys()
    anna = user()
    resolver = keys.resolver()
    assert isinstance(resolver, JwtPrincipalResolver) and resolver.revocation_events is None
    async with lifespan_client(_app(keys, None)) as client:
        response = await client.put("/preferences", headers=bearer(keys.mint(anna)))
    assert response.status_code == 200


# --- the leeway is bounded: Identity's wait depends on it ------------------------------------


def test_a_leeway_above_the_bound_is_refused_at_start_up() -> None:
    """Identity asks the services to erase an account once every token has expired, the
    verifiers' leeway included. A verifier that accepts tokens for longer would break that, so
    it does not start (ADR 0082)."""
    assert MAX_LEEWAY_SECONDS == 30.0
    assert AuthSettings(jwks_url="http://identity/jwks.json").leeway_seconds == 30.0
    env = {"MOWDECK_AUTH_JWKS_URL": "http://identity/jwks.json"}
    assert AuthSettings.from_env({**env, "MOWDECK_AUTH_LEEWAY_SECONDS": "30"}) is not None
    for bad in ("31", "120"):
        with pytest.raises(ValueError, match="MOWDECK_AUTH_LEEWAY_SECONDS"):
            AuthSettings.from_env({**env, "MOWDECK_AUTH_LEEWAY_SECONDS": bad})
    with pytest.raises(ValueError, match="between 0 and 30 s"):
        JwtVerifier(TestKeys().cache(), leeway=timedelta(seconds=45))
    assert JwtVerifier(TestKeys().cache(), leeway=timedelta(0)).leeway == timedelta(0)
