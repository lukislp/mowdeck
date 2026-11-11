# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure on PostgreSQL (ADR 0082): the sanctioned audit scrub and the purge record.

Runs with ``MOWDECK_TEST_DATABASE_URL`` like ``test_identity_db.py`` (CI has the server).
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from mowdeck_service_kit.db import async_database_url
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_identity_db import migrate, needs_db, temporary_database

from identity.adapters.db.store import SqlStore
from identity.domain.erasure import AccountErasure
from identity.domain.models import AuditEntry

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
SCRUB_FLAG = "SELECT set_config('mowdeck.audit_scrub', 'on', true)"
INSERT = text(
    "INSERT INTO audit_log (at, action, actor_subject, outcome, account_id, actor_name, "
    "target_type, target_id, details, ip, correlation_id) "
    "VALUES (now(), :action, 'user:1', 'success', :account, 'Lukas', 'user', '42', "
    "CAST(:details AS jsonb), '10.0.0.1', 'c-1')"
)
DETAILS = '{"username": "lukas", "display_name": "L", "delete_after": "2026-11-17", "sessions": 3}'


@needs_db
async def test_the_audit_scrub_removes_personal_fields_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The append-only trigger lets one UPDATE through, the scrub function's, and it only
    removes data: the rows stay and no other column changes."""
    account_id, other_id = uuid4(), uuid4()
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            async with engine.begin() as conn:
                for account in (account_id, other_id):
                    await conn.execute(
                        INSERT, {"action": "user.created", "account": account, "details": DETAILS}
                    )
            scrub = text("SELECT audit_log_scrub(:a)")
            async with engine.begin() as conn:
                changed = (await conn.execute(scrub, {"a": account_id})).scalar_one()
                again = (await conn.execute(scrub, {"a": account_id})).scalar_one()
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT account_id, action, actor_subject, actor_name, ip, "
                            "target_id, correlation_id, details FROM audit_log ORDER BY id"
                        )
                    )
                ).all()
            assert (changed, again) == (1, 0)
            scrubbed, untouched = rows
            assert scrubbed.account_id == account_id and untouched.account_id == other_id
            assert scrubbed.details == {"delete_after": "2026-11-17", "sessions": 3}
            assert (scrubbed.actor_name, scrubbed.ip) == (None, None)
            assert (scrubbed.action, scrubbed.actor_subject, scrubbed.target_id) == (
                "user.created",
                "user:1",
                "42",
            )
            assert scrubbed.correlation_id == "c-1"
            assert untouched.actor_name == "Lukas" and untouched.details["username"] == "lukas"
        finally:
            await engine.dispose()


@needs_db
async def test_the_trigger_still_refuses_everything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the flag every UPDATE fails; with it only a removal of personal data passes."""
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    INSERT, {"action": "user.created", "account": uuid4(), "details": DETAILS}
                )
            changes = (
                "UPDATE audit_log SET action = 'z.z'",
                "UPDATE audit_log SET details = '{\"new\": 1}'::jsonb",
                'UPDATE audit_log SET details = \'{"username": "mallory"}\'::jsonb',
                "UPDATE audit_log SET actor_name = 'Mallory'",
                "UPDATE audit_log SET account_id = NULL",
                "UPDATE audit_log SET at = now() + interval '1 day'",
            )
            for statement in changes:
                with pytest.raises(DBAPIError, match="append-only"):
                    async with engine.begin() as conn:
                        await conn.execute(text(statement))
                with pytest.raises(DBAPIError, match="append-only"):
                    async with engine.begin() as conn:
                        await conn.execute(text(SCRUB_FLAG))
                        await conn.execute(text(statement))
            with pytest.raises(DBAPIError, match="append-only"):
                async with engine.begin() as conn:
                    await conn.execute(text(SCRUB_FLAG))
                    await conn.execute(text("DELETE FROM audit_log"))
            # A removal of personal data passes with the flag, as the function does it.
            async with engine.begin() as conn:
                await conn.execute(text(SCRUB_FLAG))
                await conn.execute(text("UPDATE audit_log SET ip = NULL, actor_name = NULL"))
            # The function clears the flag: a later statement in the same transaction is refused.
            with pytest.raises(DBAPIError, match="append-only"):
                async with engine.begin() as conn:
                    await conn.execute(text("SELECT audit_log_scrub(gen_random_uuid())"))
                    await conn.execute(text("UPDATE audit_log SET ip = NULL"))
        finally:
            await engine.dispose()


@needs_db
async def test_the_store_scrubs_and_keeps_the_erasure_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = uuid4()
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        store = SqlStore(engine)
        try:
            erasure = AccountErasure.begin(account_id, ["fleet", "scheduler"], NOW)
            async with store.transaction() as tx:
                await tx.audit.add(
                    AuditEntry(
                        at=NOW,
                        action="account.registered",
                        actor_subject=None,
                        actor_kind=None,
                        account_id=account_id,
                        actor_name="Lukas",
                        details={"username": "lukas", "kind": "household"},
                        ip="10.0.0.1",
                    )
                )
                await tx.erasures.add(erasure)
            async with store.transaction() as tx:
                assert await tx.scrub_audit(account_id) == 1
                assert await tx.scrub_audit(account_id) == 0
                erasure.confirm("fleet", 3, NOW, wait=timedelta(0))
                await tx.erasures.update(erasure)
            async with store.transaction() as tx:
                entry = (await tx.audit.find(account_id=account_id))[0]
                record = await tx.erasures.get(account_id=account_id)
            assert entry.details == {"kind": "household"} and entry.ip is None
            assert entry.actor_name is None
            assert record is not None and record.counts == {"fleet": 3}
            assert record.open_services == ["scheduler"]
        finally:
            await engine.dispose()


@needs_db
async def test_a_whole_account_is_erased_on_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real tables: every row of the account and its user goes (the cascades), the audit
    rows stay without personal data, the other account is untouched, the record is final."""
    from identity_support import PASSWORD, harness
    from test_identity_db import store_of

    settings: dict[str, Any] = {
        "identity_erasure_enabled": True,
        "identity_deletion_grace_days": 0,
        "identity_erasure_services": "fleet",
        "identity_erasure_settle_seconds": 0,
    }
    async with (
        store_of("postgres", monkeypatch) as store,
        harness(store=store, eraser=_Eraser(), **settings) as h,
    ):
        lukas = await h.register("lukas")
        bob = await h.register("bob")
        response = await h.client.request(
            "DELETE", lukas.url(), headers=lukas.headers, json={"password": PASSWORD}
        )
        assert response.status_code == 202, response.text
        # The purge begins, waits until the access tokens of the account have expired, then asks.
        begun = await h.app.state.identity.erasure.begin_due()
        h.clock.advance(timedelta(minutes=11))
        sweep = await h.app.state.identity.erasure.sweep()
        async with store.transaction() as tx:
            account = await tx.accounts.get(id=lukas.account_id)
            user = await tx.users.get(id=lukas.user_id)
            members = await tx.memberships.find(account_id=lukas.account_id)
            roles = await tx.roles.find(account_id=lukas.account_id)
            sessions = await tx.sessions.find(user_id=lukas.user_id)
            audit = await tx.audit.find(account_id=lukas.account_id)
            record = await tx.erasures.get(account_id=lukas.account_id)
            bobs = await tx.accounts.get(id=bob.account_id)
            bobs_roles = await tx.roles.find(account_id=bob.account_id)
    assert (begun, sweep.erased) == (1, 1)
    assert (account, user, members, roles, sessions) == (None, None, [], [], [])
    assert audit and all("username" not in e.details and e.actor_name is None for e in audit)
    assert record is not None and record.state.value == "erased" and record.counts == {"fleet": 4}
    assert bobs is not None and bobs_roles


class _Eraser:
    async def erase(self, service: str, account_id: object) -> int:
        return 4
