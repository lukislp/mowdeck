"""PostgreSQL: migrations up and down, schema drift, and the store contract.

The PostgreSQL tests run when ``MOWDECK_TEST_DATABASE_URL`` points at a server where the user
may create databases (CI: ``postgresql://postgres:test@127.0.0.1:5433/postgres``). Each test
creates its own database and drops it afterwards. The store contract also runs against the
memory store, so both implementations behave the same. Locally::

    docker run -d --name vision-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test \\
        postgres:17.11-alpine
    MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \\
        uv run pytest services/vision -k db
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from mowdeck_contracts.v1 import PrincipalKind, PrincipalRef, VisionTask, VisionTrigger
from mowdeck_service_kit.db import async_database_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from vision_support import T0, answer

from vision.adapters.db.migrate import alembic_config, upgrade_with_retry
from vision.adapters.db.store import SqlStore
from vision.adapters.db.tables import metadata
from vision.adapters.memory import MemoryStore
from vision.application.ports import Store
from vision.domain.errors import ConflictError
from vision.domain.models import (
    AutoSetting,
    FrameInfo,
    Proposal,
    ProposalState,
    ProposalStep,
    ProviderRequest,
    StepOutcome,
    StoredImage,
    UserPreferences,
    UserProvider,
)

ADMIN_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="needs MOWDECK_TEST_DATABASE_URL")


@asynccontextmanager
async def temporary_database() -> AsyncIterator[str]:
    """Create a fresh database and yield its URL; drop it afterwards."""
    assert ADMIN_URL
    name = f"vision_test_{uuid4().hex[:10]}"
    admin = create_async_engine(async_database_url(ADMIN_URL), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    base, _, _ = ADMIN_URL.rpartition("/")
    try:
        yield f"{base}/{name}"
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def migrate(
    url: str, monkeypatch: pytest.MonkeyPatch, revision: str = "head", *, down: bool = False
) -> None:
    monkeypatch.setenv("MOWDECK_DATABASE_URL", url)
    if down:
        await asyncio.to_thread(command.downgrade, alembic_config(), revision)
    else:
        await asyncio.to_thread(command.upgrade, alembic_config(), revision)


async def table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return {row[0] for row in rows}


def test_vision_db_upgrade_retries_refused_connections() -> None:
    calls: list[int] = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) < 3:
            raise OSError("refused")

    upgrade_with_retry(flaky, attempts=5, delay_seconds=0, sleep=lambda _: None)
    assert len(calls) == 3
    with pytest.raises(OSError, match="refused"):
        upgrade_with_retry(
            lambda: (_ for _ in ()).throw(OSError("refused")), attempts=2, sleep=lambda _: None
        )


@needs_db
async def test_vision_db_migrations_up_down_and_no_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            assert set(metadata.tables) <= await table_names(engine)

            def diff(sync_conn: Any) -> list[Any]:
                context = MigrationContext.configure(sync_conn)
                return list(compare_metadata(context, metadata))

            async with engine.connect() as conn:
                assert await conn.run_sync(diff) == []
            await migrate(url, monkeypatch, "0001", down=True)
            assert not set(metadata.tables) & await table_names(engine)
            await migrate(url, monkeypatch)
            assert set(metadata.tables) <= await table_names(engine)
        finally:
            await engine.dispose()


def _proposal(mower_id: Any, account_id: Any, **changes: Any) -> Proposal:
    values: dict[str, Any] = {
        "id": uuid4(),
        "chain_id": uuid4(),
        "round": 1,
        "account_id": account_id,
        "mower_id": mower_id,
        "task": VisionTask.RECOVER,
        "state": ProposalState.ANALYSING,
        "requested_by": PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
        "created_at": T0,
        "updated_at": T0,
        "context": {"task": "recover"},
        "image_count": 1,
        **changes,
    }
    return Proposal(**values)


async def store_contract(store: Store) -> None:
    """Behaviour every store implementation must have."""
    account_id, mower_id = uuid4(), uuid4()
    frames = [
        FrameInfo(0, "upload", "Front left", "left", T0),
        FrameInfo(1, "upload", "Front right", "right", T0 + timedelta(milliseconds=90)),
    ]
    first = _proposal(mower_id, account_id, stereo=True, frames=frames, image_count=2)
    image = StoredImage(
        first.id, 0, "image/jpeg", b"\xff\xd8jpeg", "mower", T0 + timedelta(minutes=5)
    )
    await store.add_proposal(first, [image])
    with pytest.raises(ConflictError):
        await store.add_proposal(_proposal(mower_id, account_id), [])
    loaded = await store.get_proposal(first.id)
    assert loaded is not None and loaded.state is ProposalState.ANALYSING
    assert loaded.requested_by == first.requested_by and loaded.context == {"task": "recover"}
    assert loaded.stereo is True
    assert loaded.frames == frames
    assert await store.get_proposal(uuid4()) is None
    approver = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER, display_name="A")
    result = answer()
    assert await store.update_proposal(
        first.id,
        {
            "state": ProposalState.PROPOSED,
            "assessment": result,
            "expires_at": T0 + timedelta(minutes=5),
        },
        states={ProposalState.ANALYSING},
    )
    assert not await store.update_proposal(
        first.id, {"state": ProposalState.FAILED}, states={ProposalState.ANALYSING}
    )
    assert await store.update_proposal(
        first.id,
        {
            "state": ProposalState.EXECUTING,
            "approved_by": approver,
            "approved_at": T0,
            "start_pose": {"x": 1.0, "y": 2.0, "theta_rad": 0.5},
        },
    )
    loaded = await store.get_proposal(first.id)
    assert loaded is not None and loaded.approved_by == approver
    assert loaded.assessment == result and loaded.start_pose == {
        "x": 1.0,
        "y": 2.0,
        "theta_rad": 0.5,
    }
    opened = await store.open_proposal(mower_id)
    assert opened is not None and opened.id == first.id
    await store.put_steps([ProposalStep(first.id, i, -0.5, 0.0, 1.0) for i in range(2)])
    await store.update_step(first.id, 0, {"outcome": StepOutcome.DONE, "frames": 5, "ended_at": T0})
    steps = await store.steps(first.id)
    assert [(s.outcome, s.frames) for s in steps] == [
        (StepOutcome.DONE, 5),
        (StepOutcome.PENDING, 0),
    ]
    assert await store.update_proposal(
        first.id, {"state": ProposalState.RECOVERED, "updated_at": T0}
    )
    assert await store.open_proposal(mower_id) is None
    second = _proposal(
        mower_id,
        account_id,
        chain_id=first.chain_id,
        round=2,
        parent_id=first.id,
        created_at=T0 + timedelta(minutes=1),
        updated_at=T0 + timedelta(minutes=1),
    )
    await store.add_proposal(second, [])
    assert [p.round for p in await store.chain(first.chain_id)] == [1, 2]
    listed = await store.list_proposals(account_id, mower_id, limit=10)
    assert [p.id for p in listed] == [second.id, first.id]
    assert [
        p.id
        for p in await store.list_proposals(
            account_id, mower_id, limit=10, before=second.created_at
        )
    ] == [first.id]
    assert await store.list_proposals(uuid4(), mower_id, limit=10) == []
    stale = await store.proposals_in(
        {ProposalState.ANALYSING}, updated_before=T0 + timedelta(hours=1)
    )
    assert [p.id for p in stale] == [second.id]
    assert (await store.get_image(first.id, 0)) == image
    assert await store.get_image(first.id, 1) is None
    assert await store.purge_images(T0) == 0
    assert await store.purge_images(T0 + timedelta(minutes=6)) == 1
    user_id = uuid4()
    for kind, hosted in (("analysis", True), ("analysis", False), ("test", False)):
        await store.record_request(
            ProviderRequest(
                id=uuid4(),
                account_id=account_id,
                user_id=user_id,
                kind=kind,  # type: ignore[arg-type]
                model="fake/recover",
                family="fake",
                hosted=hosted,
                outcome="ok",
                created_at=T0,
                proposal_id=first.id,
                input_tokens=10,
                output_tokens=2,
                cost_usd=0.001,
                latency_ms=5,
            )
        )
    assert await store.count_requests(since=T0, kind="analysis", user_id=user_id) == 2
    assert (
        await store.count_requests(since=T0, kind="analysis", account_id=account_id, hosted=True)
        == 1
    )
    assert await store.count_requests(since=T0 + timedelta(seconds=1), kind="analysis") == 0
    assert await store.count_requests(since=T0, kind="test") == 1
    provider = UserProvider(
        user_id,
        account_id,
        "openai/gpt-5-mini",
        True,
        T0,
        T0,
        api_base="https://llm.example.org/v1",
        api_key_ciphertext=b"\x01\x02",
        header_names=["X-A"],
    )
    await store.put_provider(provider)
    provider.enabled = False
    provider.last_test = {"ok": True}
    await store.put_provider(provider)
    loaded_provider = await store.get_provider(user_id, account_id)
    assert loaded_provider is not None and loaded_provider.enabled is False
    assert loaded_provider.api_key_ciphertext == b"\x01\x02" and loaded_provider.header_names == [
        "X-A"
    ]
    assert loaded_provider.last_test == {"ok": True}
    assert await store.get_provider(user_id, uuid4()) is None
    assert await store.delete_provider(user_id, account_id)
    assert not await store.delete_provider(user_id, account_id)
    await store.put_provider(provider)
    assert await store.get_preferences(user_id, account_id) is None
    await store.put_preferences(UserPreferences(user_id, account_id, False, T0))
    await store.put_preferences(UserPreferences(user_id, account_id, True, T0))
    prefs = await store.get_preferences(user_id, account_id)
    assert prefs is not None and prefs.stereo_frames is True and prefs.updated_at == T0
    # One row holds both preferences: each writer keeps the other's column.
    await store.put_auto_setting(AutoSetting(user_id, account_id, True, T0))
    prefs = await store.get_preferences(user_id, account_id)
    assert prefs is not None and prefs.stereo_frames is True
    await store.put_preferences(UserPreferences(user_id, account_id, False, T0))
    opt_in = await store.get_auto_setting(user_id, account_id)
    assert opt_in is not None and opt_in.enabled is True
    assert await store.delete_user(user_id) == 1
    assert await store.get_preferences(user_id, account_id) is None
    assert await store.get_auto_setting(user_id, account_id) is None
    await auto_contract(store)


async def auto_contract(store: Store) -> None:
    """Opt-ins, the error watch and the proposal counts of automatic analyses."""
    account_id, mower_id = uuid4(), uuid4()
    early, late = uuid4(), uuid4()
    await store.put_auto_setting(AutoSetting(late, account_id, True, T0 + timedelta(minutes=1)))
    await store.put_auto_setting(
        AutoSetting(early, account_id, True, T0, kind=PrincipalKind.SUBUSER)
    )
    await store.put_auto_setting(AutoSetting(uuid4(), account_id, False, T0))
    await store.put_auto_setting(AutoSetting(uuid4(), uuid4(), True, T0))
    subscribers = await store.auto_subscribers(account_id)
    assert [(s.user_id, s.kind) for s in subscribers] == [
        (early, PrincipalKind.SUBUSER),
        (late, PrincipalKind.USER),
    ]
    await store.put_auto_setting(AutoSetting(late, account_id, False, T0 + timedelta(minutes=2)))
    assert [s.user_id for s in await store.auto_subscribers(account_id)] == [early]
    loaded = await store.get_auto_setting(late, account_id)
    assert loaded is not None and loaded.enabled is False
    assert await store.swap_auto_watch(mower_id, account_id, None, T0) is None
    assert await store.swap_auto_watch(mower_id, account_id, "1201", T0) is None
    assert await store.swap_auto_watch(mower_id, account_id, "1201", T0) == "1201"
    assert await store.swap_auto_watch(mower_id, account_id, None, T0) == "1201"
    automatic = _proposal(
        mower_id,
        account_id,
        trigger=VisionTrigger.ERROR,
        trigger_error_code="1201",
        state=ProposalState.REJECTED,
        created_at=T0 + timedelta(minutes=5),
    )
    await store.add_proposal(automatic, [])
    await store.add_proposal(_proposal(mower_id, account_id, created_at=T0), [])
    loaded_proposal = await store.get_proposal(automatic.id)
    assert loaded_proposal is not None and loaded_proposal.trigger is VisionTrigger.ERROR
    assert loaded_proposal.trigger_error_code == "1201"
    assert await store.count_proposals(mower_id, since=T0) == 2
    assert await store.count_proposals(mower_id, since=T0, trigger=VisionTrigger.ERROR) == 1
    assert await store.count_proposals(mower_id, since=T0 + timedelta(minutes=6)) == 0


async def test_vision_db_memory_store_contract() -> None:
    await store_contract(MemoryStore())


@needs_db
async def test_vision_db_sql_store_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            await store_contract(SqlStore(engine))
        finally:
            await engine.dispose()
