# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

""" "Delete me" on PostgreSQL (ADR 0082, step 3): the audit scrub of one user through the
sanctioned path of migration 0012, the widened trigger rule, and a member's deletion on the real
tables.

Runs with ``MOWDECK_TEST_DATABASE_URL`` like ``test_identity_db.py`` (CI has the server).
"""

from uuid import uuid4

import pytest
from mowdeck_service_kit.db import async_database_url, user_tables
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_identity_db import migrate, needs_db, store_of, temporary_database

from identity.adapters.db.store import SqlStore, SqlTx
from identity.adapters.db.tables import metadata
from identity.domain.erasure import audit_fields_after_user_scrub

SCRUB_FLAG = "SELECT set_config('mowdeck.audit_scrub', 'on', true)"
INSERT = text(
    "INSERT INTO audit_log (at, action, actor_subject, outcome, account_id, actor_name, "
    "target_type, target_id, details, ip, correlation_id) "
    "VALUES (now(), :action, :actor, 'success', :account, :name, :target_type, :target, "
    "CAST(:details AS jsonb), '10.0.0.1', 'c-1')"
)
DETAILS = '{"username": "anna", "device": "Firefox", "subuser": true, "session": "s-1"}'
KEPT = {"subuser": True, "session": "s-1"}
READ = text(
    "SELECT action, actor_subject, actor_name, ip, target_id, correlation_id, details "
    "FROM audit_log ORDER BY id"
)


@needs_db
async def test_the_user_scrub_removes_the_users_personal_fields_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rows the user acted in or is the target of lose the personal ``details`` keys; the
    name and address go when they are the user's and stay when another actor is named. Every
    other row and column is untouched, and the SQL function decides as the domain rule does."""
    account_id, anna, lukas = uuid4(), uuid4(), uuid4()
    rows = [
        # action, actor, actor name, target type, target
        ("session.login", None, None, "user", str(anna)),  # a sign-in: the address is Anna's
        ("user.renamed", f"user:{anna}", "Anna", "user", str(anna)),  # Anna on herself
        ("role.created", f"user:{anna}", "Anna", "role", "r-1"),  # Anna on something else
        ("user.created", f"user:{lukas}", "Lukas", "user", str(anna)),  # an admin on Anna
        ("user.created", f"user:{lukas}", "Lukas", "user", str(uuid4())),  # not about Anna
        ("session.login", None, None, "user", str(lukas)),  # somebody else's sign-in
    ]
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            async with engine.begin() as conn:
                for action, actor, name, target_type, target in rows:
                    await conn.execute(
                        INSERT,
                        {
                            "action": action,
                            "actor": actor,
                            "account": account_id,
                            "name": name,
                            "target_type": target_type,
                            "target": target,
                            "details": DETAILS,
                        },
                    )
            scrub = text("SELECT audit_log_scrub_user(:u)")
            async with engine.begin() as conn:
                changed = (await conn.execute(scrub, {"u": anna})).scalar_one()
                again = (await conn.execute(scrub, {"u": anna})).scalar_one()
            async with engine.connect() as conn:
                after = (await conn.execute(READ)).all()
        finally:
            await engine.dispose()
    assert (changed, again) == (4, 0)
    login, renamed, role, by_admin, other_user, other_login = after
    for row in (login, renamed, role):
        assert (row.actor_name, row.ip, row.details) == (None, None, KEPT)
    assert (by_admin.actor_name, by_admin.ip, by_admin.details) == ("Lukas", "10.0.0.1", KEPT)
    for row in (other_user, other_login):
        assert row.ip == "10.0.0.1" and row.details["username"] == "anna"
    assert other_user.actor_name == "Lukas"
    # Nothing but the personal columns moved.
    assert [(r.action, r.actor_subject, r.target_id, r.correlation_id) for r in after] == [
        (action, actor, target, "c-1") for action, actor, _, _, target in rows
    ]
    # The domain rule (the memory store's) says the same for every row about Anna.
    for (_, actor, name, _, _), row in zip(rows[:4], after[:4], strict=True):
        assert (row.actor_name, row.ip, row.details) == audit_fields_after_user_scrub(
            anna,
            actor_subject=actor,
            actor_name=name,
            ip="10.0.0.1",
            details={"username": "anna", "device": "Firefox", "subuser": True, "session": "s-1"},
        )


@needs_db
async def test_the_trigger_lets_personal_columns_stay_or_go_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Migration 0012 widened one thing: with the flag, the name and the address may each stay.
    They still cannot change, nothing can be added, and without the flag nothing passes."""
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    INSERT,
                    {
                        "action": "user.created",
                        "actor": "user:1",
                        "account": uuid4(),
                        "name": "Lukas",
                        "target_type": "user",
                        "target": "42",
                        "details": DETAILS,
                    },
                )
            refused = (
                "UPDATE audit_log SET actor_name = 'Mallory'",
                "UPDATE audit_log SET ip = '10.9.9.9'",
                "UPDATE audit_log SET actor_subject = NULL",
                "UPDATE audit_log SET target_id = '43'",
                "UPDATE audit_log SET details = details || '{\"new\": 1}'::jsonb",
                "UPDATE audit_log SET details = details - 'username', action = 'z.z'",
            )
            for statement in refused:
                with pytest.raises(DBAPIError, match="append-only"):
                    async with engine.begin() as conn:
                        await conn.execute(text(SCRUB_FLAG))
                        await conn.execute(text(statement))
            for statement in (
                "UPDATE audit_log SET details = details - 'username'",
                "UPDATE audit_log SET ip = NULL",
            ):
                with pytest.raises(DBAPIError, match="append-only"):
                    async with engine.begin() as conn:
                        await conn.execute(text(statement))  # no flag
            async with engine.begin() as conn:
                await conn.execute(text(SCRUB_FLAG))
                await conn.execute(text("UPDATE audit_log SET details = details - 'username'"))
                await conn.execute(text("UPDATE audit_log SET ip = NULL"))
            async with engine.connect() as conn:
                row = (await conn.execute(READ)).one()
            # The function clears the flag: a later statement in the same transaction is refused.
            with pytest.raises(DBAPIError, match="append-only"):
                async with engine.begin() as conn:
                    await conn.execute(text("SELECT audit_log_scrub_user(gen_random_uuid())"))
                    await conn.execute(text("UPDATE audit_log SET ip = NULL"))
        finally:
            await engine.dispose()
    assert (row.actor_name, row.ip) == ("Lukas", None) and "username" not in row.details


@needs_db
async def test_the_downgrade_restores_the_stricter_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        await migrate(url, monkeypatch, "0011", down=True)
        engine = create_async_engine(async_database_url(url))
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    INSERT,
                    {
                        "action": "user.created",
                        "actor": "user:1",
                        "account": uuid4(),
                        "name": "Lukas",
                        "target_type": "user",
                        "target": "42",
                        "details": DETAILS,
                    },
                )
            async with engine.connect() as conn:
                functions = (
                    await conn.execute(
                        text("SELECT proname FROM pg_proc WHERE proname LIKE 'audit_log_scrub%'")
                    )
                ).scalars()
                names = set(functions)
            with pytest.raises(DBAPIError, match="append-only"):
                async with engine.begin() as conn:
                    await conn.execute(text(SCRUB_FLAG))
                    await conn.execute(text("UPDATE audit_log SET ip = NULL"))  # the name stays
            await migrate(url, monkeypatch)
        finally:
            await engine.dispose()
    assert names == {"audit_log_scrub"}


@needs_db
async def test_a_member_is_deleted_on_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real tables and constraints: every row of the user goes, the audit rows stay without
    the user's personal fields, the owner, the other member and the other account keep theirs."""
    from identity_support import PASSWORD, bearer, harness

    async with store_of("postgres", monkeypatch) as store, harness(store=store) as h:
        lukas, bob = await h.register("lukas"), await h.register("bob")
        operator = await h.role_id(lukas, "operator")
        level = {"type": "account", "id": str(lukas.account_id)}
        anna_id, anna = await h.subuser(lukas, "anna", [{"role_id": str(operator), "level": level}])
        ben_id, _ = await h.subuser(lukas, "ben")
        personal = await h.client.post(
            "/api/v1/identity/me/api-tokens",
            headers=bearer(anna),
            json={"name": "script", "scopes": ["mower.view"], "expires_in_days": 30},
        )
        assert personal.status_code == 201, personal.text
        response = await h.client.request(
            "DELETE", "/api/v1/identity/me", headers=bearer(anna), json={"password": PASSWORD}
        )
        async with store.transaction() as tx:
            gone = (
                await tx.users.get(id=anna_id),
                await tx.memberships.find(user_id=anna_id),
                await tx.sessions.find(user_id=anna_id),
                await tx.assignments.find(subject_id=anna_id),
                await tx.api_tokens.find(created_by=anna_id),
                await tx.login_attempts.find(username_norm="anna"),
            )
            kept = (
                await tx.users.get(id=ben_id),
                await tx.users.get(id=lukas.user_id),
                await tx.users.get(id=bob.user_id),
                await tx.accounts.get(id=lukas.account_id),
            )
            about = await tx.audit.find(target_type="user", target_id=str(anna_id))
            own = await tx.audit.find(actor_subject=f"user:{anna_id}")
            again = await tx.scrub_user_audit(anna_id)
        # Read from the schema: no table with a user_id column holds a row of the user.
        assert isinstance(store, SqlStore)
        async with store.engine.connect() as conn:
            left = {
                table.name: (
                    await conn.execute(
                        select(func.count()).select_from(table).where(table.c.user_id == anna_id)
                    )
                ).scalar_one()
                for table in user_tables(metadata)
            }
        relogin = await h.client.post(
            "/api/v1/identity/session", json={"username": "anna", "password": PASSWORD}
        )
    assert response.status_code == 200, response.text
    assert gone == (None, [], [], [], [], [])
    assert len(left) >= 8 and not any(left.values()), left
    assert all(row is not None for row in kept)
    assert {"user.created", "session.login", "user.self_deleted"} <= {e.action for e in about}
    assert own and all((e.actor_name, e.ip) == (None, None) for e in own)
    assert all("username" not in e.details and "name" not in e.details for e in [*about, *own])
    created = next(e for e in about if e.action == "user.created")
    assert created.actor_name == "lukas"  # the admin's name stays on the admin's row
    assert again == 0
    assert relogin.status_code == 401


@needs_db
@pytest.mark.parametrize("scrubbed", [True, False], ids=["as shipped", "without the scrub"])
async def test_the_row_of_the_deletion_itself_keeps_no_name_and_no_address(
    scrubbed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``user.self_deleted`` is written with the caller as the actor (name and address) and the
    scrub runs after it in the same transaction, so the committed row has neither. The second
    run takes the scrub away and finds both in the row: it is the scrub that removes them, and
    the row is in the table before the scrub runs, not queued until the commit."""
    from identity_support import PASSWORD, bearer, harness

    async def no_scrub(self: SqlTx, user_id: object) -> int:
        del self, user_id
        return 0

    if not scrubbed:
        monkeypatch.setattr(SqlTx, "scrub_user_audit", no_scrub)
    async with store_of("postgres", monkeypatch) as store, harness(store=store) as h:
        lukas = await h.register("lukas")
        anna_id, anna = await h.subuser(lukas, "anna")
        response = await h.client.request(
            "DELETE", "/api/v1/identity/me", headers=bearer(anna), json={"password": PASSWORD}
        )
        assert isinstance(store, SqlStore)
        async with store.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT actor_subject, actor_name, ip, target_id, details "
                        "FROM audit_log WHERE action = 'user.self_deleted'"
                    )
                )
            ).all()
    assert response.status_code == 200, response.text
    [row] = rows
    assert (row.actor_subject, row.target_id) == (f"user:{anna_id}", str(anna_id))
    assert row.details == {"subuser": True}
    if scrubbed:
        assert (row.actor_name, row.ip) == (None, None)
    else:
        assert row.actor_name == "anna" and row.ip is not None
