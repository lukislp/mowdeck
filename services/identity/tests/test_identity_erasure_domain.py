# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The rules of account erasure (ADR 0082) without storage or bus."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from identity.domain.erasure import (
    AUDIT_DETAILS_KEPT,
    AccountErasure,
    ErasureState,
    deletion_blocked_by,
    grace_over,
    other_members,
    retry_delay,
)
from identity.domain.models import Account, AccountStatus, Membership, Status

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/identity/adapters/db/migrations/versions/0011_account_erasure.py"
)


def _member(user_id: UUID, *, owner: bool = False, status: Status = Status.ACTIVE) -> Membership:
    return Membership(uuid4(), uuid4(), user_id, status, NOW, is_owner=owner)


def _account(status: AccountStatus, delete_after: datetime | None) -> Account:
    return Account(uuid4(), "n", "n", "household", status, "free", NOW, delete_after)


def test_only_the_owner_alone_may_delete_the_account() -> None:
    owner, anna, bob = uuid4(), uuid4(), uuid4()
    alone = [_member(owner, owner=True)]
    with_anna = [*alone, _member(anna)]
    disabled = [*alone, _member(bob, status=Status.DISABLED)]
    assert deletion_blocked_by(alone, owner) == 0
    assert deletion_blocked_by(with_anna, owner) == 1
    assert deletion_blocked_by(disabled, owner) == 1  # a disabled member is still a person
    assert [m.user_id for m in other_members(with_anna, owner)] == [anna]


def test_the_grace_period_is_over_only_for_a_scheduled_deletion() -> None:
    due = _account(AccountStatus.DELETING, NOW - timedelta(seconds=1))
    assert grace_over(due, NOW)
    assert grace_over(_account(AccountStatus.DELETING, NOW), NOW)
    assert not grace_over(_account(AccountStatus.DELETING, NOW + timedelta(seconds=1)), NOW)
    assert not grace_over(_account(AccountStatus.ACTIVE, None), NOW)
    assert not grace_over(_account(AccountStatus.PURGING, NOW - timedelta(days=1)), NOW)
    assert not grace_over(_account(AccountStatus.DELETING, None), NOW)


def test_the_retry_delay_doubles_and_is_capped() -> None:
    cap = timedelta(minutes=10)
    assert retry_delay(1, cap) == timedelta(seconds=30)
    assert retry_delay(2, cap) == timedelta(seconds=60)
    assert retry_delay(0, cap) == timedelta(seconds=30)
    assert retry_delay(6, cap) == cap
    assert retry_delay(500, cap) == cap


def test_a_purge_starts_due_with_the_services_once_each() -> None:
    erasure = AccountErasure.begin(uuid4(), ["fleet", "scheduler", "fleet"], NOW)
    assert erasure.required == ["fleet", "scheduler"]
    assert erasure.state is ErasureState.PURGING
    assert erasure.due(NOW) and erasure.open_services == ["fleet", "scheduler"]
    assert not erasure.all_confirmed


def test_confirming_is_idempotent_and_orders_the_open_services() -> None:
    erasure = AccountErasure.begin(uuid4(), ["fleet", "scheduler"], NOW)
    erasure.attempts, erasure.last_error = 3, "fleet: down"
    erasure.confirm("fleet", 5, NOW, wait=timedelta(seconds=30))
    erasure.confirm("fleet", 9, NOW, wait=timedelta(0))
    assert erasure.confirmed == ["fleet"] and erasure.counts == {"fleet": 5}
    assert erasure.attempts == 0 and erasure.last_error is None
    assert erasure.open_services == ["scheduler"]
    erasure.confirm("scheduler", -4, NOW, wait=timedelta(0))
    assert erasure.all_confirmed and erasure.counts["scheduler"] == 0
    with pytest.raises(ValueError, match="not a required service"):
        erasure.confirm("billing", 1, NOW, wait=timedelta(0))


def test_a_failure_backs_off_and_keeps_the_reason() -> None:
    erasure = AccountErasure.begin(uuid4(), ["fleet"], NOW)
    erasure.fail("fleet", "x" * 600, NOW, maximum=timedelta(minutes=10))
    assert erasure.attempts == 1 and erasure.last_error is not None
    assert len(erasure.last_error) == 500 and erasure.last_error.startswith("fleet: xx")
    assert erasure.next_attempt_at == NOW + timedelta(seconds=30)
    assert not erasure.due(NOW) and erasure.due(NOW + timedelta(seconds=30))
    erasure.lease(NOW, timedelta(minutes=5))
    assert not erasure.due(NOW + timedelta(minutes=4)) and erasure.due(NOW + timedelta(minutes=5))


def test_a_finished_purge_is_never_due() -> None:
    erasure = AccountErasure.begin(uuid4(), ["fleet"], NOW)
    erasure.fail("fleet", "down", NOW, maximum=timedelta(minutes=1))
    erasure.finish(NOW + timedelta(hours=1))
    assert erasure.state is ErasureState.ERASED
    assert erasure.completed_at == NOW + timedelta(hours=1)
    assert erasure.last_error is None and erasure.attempts == 0
    assert not erasure.due(NOW + timedelta(days=1))


def test_the_record_accepts_the_stored_state_string() -> None:
    erasure = AccountErasure(uuid4(), NOW, ["fleet"], state="erased")  # type: ignore[arg-type]
    assert erasure.state is ErasureState.ERASED


def test_the_migration_keeps_the_same_audit_keys_as_the_domain() -> None:
    spec = importlib.util.spec_from_file_location("migration_0011", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    in_sql = {part.strip().strip("'") for part in module.KEPT[len("ARRAY[") : -1].split(",")}
    assert in_sql == set(AUDIT_DETAILS_KEPT)


def test_no_personal_key_is_kept() -> None:
    personal = {"username", "name", "display_name", "email", "from", "to", "device", "client"}
    assert not personal & AUDIT_DETAILS_KEPT
