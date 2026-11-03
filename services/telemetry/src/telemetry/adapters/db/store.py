"""PostgreSQL store on SQLAlchemy Core (async, asyncpg).

Writes run in one transaction per message: the latest row is read ``FOR UPDATE``, history,
position and timeline inserts are ``ON CONFLICT DO NOTHING`` (redeliveries change nothing), and
the latest row is upserted with ``WHERE observed_at <= excluded.observed_at`` so an older
message never overwrites a newer one even when two replicas race. Downsampling is a
``date_bin`` query with the same origin as :func:`telemetry.domain.rules.bin_start`.
"""

import dataclasses
from collections.abc import AsyncGenerator, AsyncIterator, Collection
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from mowdeck_service_kit import get_logger
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from telemetry.adapters.db import tables
from telemetry.adapters.db.reads import read_connection
from telemetry.application.ports import (
    IngestRows,
    RetentionScope,
    RetentionTarget,
    StateWrites,
    ThinResult,
    Tx,
)
from telemetry.domain.health import BladeChange, DailyRow, HealthRow
from telemetry.domain.models import (
    CommandEvent,
    DirectoryEntry,
    HistoryPoint,
    HistoryRow,
    LatestRow,
    PositionRow,
    SnapshotRow,
)
from telemetry.domain.thinning import (
    CONTEXT,
    METRES_PER_DEGREE,
    MOVING_METRES,
    TURN_COS,
    TURN_MIN_STEP_M,
    ThinPolicy,
)

_log = get_logger("telemetry.db")

RETENTION_LOCK_KEY = 0x4D44_5445_4C45_0001
"""Key of the session level advisory lock that elects the replica that deletes."""

_TIME_COLUMN = {
    RetentionTarget.HISTORY: "observed_at",
    RetentionTarget.POSITIONS: "observed_at",
    RetentionTarget.COMMANDS: "at",
}

_BINNED_HISTORY = sa.text(
    """
    WITH h AS (
      SELECT date_bin(:step, observed_at, TIMESTAMPTZ '2000-01-01 00:00:00+00') AS at,
             (array_agg(activity ORDER BY observed_at DESC))[1] AS activity,
             round(avg(battery_percent))::int AS battery_percent,
             min(battery_percent)::int AS battery_min,
             max(battery_percent)::int AS battery_max,
             bool_or(charging) AS charging,
             (array_agg(rtk_fix ORDER BY observed_at DESC))[1] AS rtk_fix,
             (array_agg(error_code ORDER BY observed_at DESC)
                FILTER (WHERE error_code IS NOT NULL))[1] AS error_code,
             count(*)::int AS samples
        FROM mower_history
       WHERE account_id = :account_id AND mower_id = :mower_id
         AND observed_at >= :start AND observed_at < :end
       GROUP BY 1
    ), p AS (
      SELECT date_bin(:step, observed_at, TIMESTAMPTZ '2000-01-01 00:00:00+00') AS at,
             (array_agg(lat ORDER BY observed_at DESC))[1] AS lat,
             (array_agg(lon ORDER BY observed_at DESC))[1] AS lon
        FROM mower_positions
       WHERE account_id = :account_id AND mower_id = :mower_id
         AND observed_at >= :start AND observed_at < :end
       GROUP BY 1
    )
    SELECT h.at, h.activity, h.battery_percent, h.battery_min, h.battery_max, h.charging,
           h.rtk_fix, h.error_code, h.samples, p.lat, p.lon
      FROM h LEFT JOIN p ON p.at = h.at
     ORDER BY h.at
     LIMIT :limit
    """
).bindparams(
    sa.bindparam("step", type_=sa.Interval()),
    sa.bindparam("account_id", type_=sa.Uuid()),
    sa.bindparam("mower_id", type_=sa.Uuid()),
    sa.bindparam("start", type_=sa.DateTime(timezone=True)),
    sa.bindparam("end", type_=sa.DateTime(timezone=True)),
    sa.bindparam("limit", type_=sa.Integer()),
)


_THIN_SLICE = sa.text(
    """
    WITH w AS (
      SELECT observed_at, blades_on, rtk_fix, lat, lon,
             lag(observed_at) OVER o AS prev_t, lead(observed_at) OVER o AS next_t,
             lag(blades_on) OVER o AS prev_b, lead(blades_on) OVER o AS next_b,
             lag(rtk_fix) OVER o AS prev_f, lead(rtk_fix) OVER o AS next_f,
             lag(lat) OVER o AS prev_lat, lag(lon) OVER o AS prev_lon,
             lead(lat) OVER o AS next_lat, lead(lon) OVER o AS next_lon
        FROM mower_positions
       WHERE mower_id = :mower_id AND observed_at >= :ctx_start AND observed_at < :ctx_end
      WINDOW o AS (ORDER BY observed_at)
    ), s AS (
      SELECT w.*,
             (lon - prev_lon) * :mpd * cos(radians(lat)) AS dx1, (lat - prev_lat) * :mpd AS dy1,
             (next_lon - lon) * :mpd * cos(radians(lat)) AS dx2, (next_lat - lat) * :mpd AS dy2
        FROM w
    ), a AS (
      SELECT s.*,
             (blades_on IS TRUE
              OR (prev_lat IS NOT NULL AND sqrt(dx1 * dx1 + dy1 * dy1) > :moving)
             ) AS active,
             (prev_lat IS NOT NULL AND next_lat IS NOT NULL
              AND sqrt(dx1 * dx1 + dy1 * dy1) >= :turn_min
              AND sqrt(dx2 * dx2 + dy2 * dy2) >= :turn_min
              AND dx1 * dx2 + dy1 * dy2
                  < :turn_cos * sqrt(dx1 * dx1 + dy1 * dy1) * sqrt(dx2 * dx2 + dy2 * dy2)
             ) AS turn
        FROM s
    ), k AS (
      SELECT a.*,
             row_number() OVER (
               PARTITION BY active,
                 date_bin((CASE WHEN active THEN :active_s ELSE :idle_s END) * INTERVAL '1 second',
                          observed_at, TIMESTAMPTZ '2000-01-01 00:00:00+00')
               ORDER BY observed_at) = 1 AS first_in_bin
        FROM a
    ), gone AS (
      DELETE FROM mower_positions d USING k
       WHERE d.mower_id = :mower_id AND d.observed_at = k.observed_at
         AND k.observed_at >= :start AND k.observed_at < :end
         AND NOT k.first_in_bin
         AND NOT k.turn
         AND NOT (k.prev_t IS NULL OR k.next_t IS NULL
                  OR k.observed_at - k.prev_t > :gap OR k.next_t - k.observed_at > :gap
                  OR k.blades_on IS DISTINCT FROM k.prev_b
                  OR k.blades_on IS DISTINCT FROM k.next_b
                  OR k.rtk_fix IS DISTINCT FROM k.prev_f
                  OR k.rtk_fix IS DISTINCT FROM k.next_f)
         AND NOT EXISTS (SELECT 1 FROM mower_history h
                          WHERE h.mower_id = :mower_id AND h.observed_at = k.observed_at
                            AND h.error_code IS NOT NULL)
      RETURNING 1
    )
    SELECT (SELECT count(*) FROM gone)::int AS deleted,
           (SELECT count(*) FROM k WHERE observed_at >= :start AND observed_at < :end)::int
             AS scanned
    """
).bindparams(
    sa.bindparam("mower_id", type_=sa.Uuid()),
    sa.bindparam("start", type_=sa.DateTime(timezone=True)),
    sa.bindparam("end", type_=sa.DateTime(timezone=True)),
    sa.bindparam("ctx_start", type_=sa.DateTime(timezone=True)),
    sa.bindparam("ctx_end", type_=sa.DateTime(timezone=True)),
    sa.bindparam("gap", type_=sa.Interval()),
    sa.bindparam("active_s", type_=sa.Integer()),
    sa.bindparam("idle_s", type_=sa.Integer()),
    sa.bindparam("mpd", type_=sa.Double()),
    sa.bindparam("moving", type_=sa.Double()),
    sa.bindparam("turn_min", type_=sa.Double()),
    sa.bindparam("turn_cos", type_=sa.Double()),
)
"""One slice of the position thinning; mirrors :func:`telemetry.domain.thinning.keep_set`."""


_ACCOUNT_TABLES = (
    tables.mower_history,
    tables.mower_positions,
    tables.command_timeline,
    tables.mower_snapshots,
    tables.mower_health_daily,
    tables.mower_health,
    tables.blade_changes,
    tables.mower_latest,
    tables.mower_directory,
)
"""The tables of :class:`SqlStore` with an ``account_id`` column (the demo purge)."""

_COPIED_TABLES = (
    tables.mower_history,
    tables.mower_positions,
    tables.mower_positions_thinning,
    tables.mower_health_daily,
    tables.mower_health,
    tables.mower_latest,
)
"""What the ingestion writes of a mower's states (``copy_ingested``, ADR 0079)."""


def _with_ids(state: sa.ColumnElement[Any], mower_id: UUID, account_id: UUID) -> Any:
    """``state`` (a ``MowerState`` as JSONB) naming another mower and account."""
    for key, value in (("mower_id", mower_id), ("account_id", account_id)):
        state = sa.func.jsonb_set(
            state,
            sa.literal([key], ARRAY(sa.Text())),
            sa.func.to_jsonb(sa.cast(str(value), sa.Text())),
        )
    return state


_POSITION_COLUMNS = tuple(
    tables.mower_positions.c[field.name] for field in dataclasses.fields(PositionRow)
)
"""The columns of ``mower_positions`` in the field order of :class:`PositionRow`."""


def _upsert_statement(
    table: sa.Table, row_type: type, keys: tuple[str, ...], *, newer_only: bool = False
) -> Any:
    """``INSERT ... ON CONFLICT (keys) DO UPDATE SET <field> = excluded.<field>`` for every
    other field of ``row_type`` (a dataclass whose fields are columns), built once and executed
    with one parameter dict per row (:func:`_fields`).

    Building the statement per message (``values(...)``, ``excluded``) was a large share of the
    ingestion's CPU (docs/ops/load-testing.md, "Ingestion cost"); a prebuilt statement also hits
    SQLAlchemy's compiled cache at once. Columns that are no field keep their stored value.
    ``newer_only`` keeps a newer stored row (``WHERE observed_at <= excluded.observed_at``)."""
    insert = pg_insert(table)
    names = [f.name for f in dataclasses.fields(row_type) if f.name not in keys]
    return insert.on_conflict_do_update(
        index_elements=list(keys),
        set_={name: insert.excluded[name] for name in names},
        where=table.c.observed_at <= insert.excluded.observed_at if newer_only else None,
    )


def _insert_new(table: sa.Table, keys: tuple[str, ...]) -> Any:
    """``INSERT ... ON CONFLICT (keys) DO NOTHING``, built once (redeliveries change nothing)."""
    return pg_insert(table).on_conflict_do_nothing(index_elements=list(keys))


_PUT_LATEST = _upsert_statement(tables.mower_latest, LatestRow, ("mower_id",), newer_only=True)
_PUT_HEALTH = _upsert_statement(tables.mower_health, HealthRow, ("mower_id",))
_PUT_DAILY = _upsert_statement(tables.mower_health_daily, DailyRow, ("mower_id", "day"))
_PUT_SNAPSHOT = _upsert_statement(
    tables.mower_snapshots, SnapshotRow, ("mower_id", "kind"), newer_only=True
)
_ADD_HISTORY = _insert_new(tables.mower_history, ("mower_id", "observed_at"))
_ADD_POSITION = _insert_new(tables.mower_positions, ("mower_id", "observed_at"))
_ADD_COMMAND_EVENT = _insert_new(tables.command_timeline, ("command_id", "status"))
_ADD_BLADE_CHANGE = _insert_new(tables.blade_changes, ("id",))

_IDS = sa.bindparam("ids", type_=ARRAY(sa.Uuid()))
_MOWER = sa.bindparam("mower_id", type_=sa.Uuid())

_OWNERS = sa.select(tables.mower_directory.c.mower_id, tables.mower_directory.c.account_id).where(
    tables.mower_directory.c.mower_id == sa.any_(_IDS)
)
_LATEST_KEYS = tuple(c for c in tables.mower_latest.columns if c.name != "state")
_LATEST_LOCKED = (
    sa.select(*_LATEST_KEYS)
    .where(tables.mower_latest.c.mower_id == sa.any_(_IDS))
    .order_by(tables.mower_latest.c.mower_id)
    .with_for_update()
)
"""The latest rows of a batch's mowers, locked, without the stored state document: the write
rules need only the keys and fingerprints, and decoding the documents was the largest cost of
reading them."""
_HEALTH_LOCKED = (
    sa.select(tables.mower_health)
    .where(tables.mower_health.c.mower_id == sa.any_(_IDS))
    .order_by(tables.mower_health.c.mower_id)
    .with_for_update()
)
_LATEST_ONE = (
    sa.select(tables.mower_latest).where(tables.mower_latest.c.mower_id == _MOWER).with_for_update()
)
_HEALTH_ONE = (
    sa.select(tables.mower_health).where(tables.mower_health.c.mower_id == _MOWER).with_for_update()
)
_DIRECTORY_ACCOUNT = sa.select(tables.mower_directory.c.account_id).where(
    tables.mower_directory.c.mower_id == _MOWER
)
_DAILY = sa.select(tables.mower_health_daily).where(
    tables.mower_health_daily.c.mower_id == _MOWER,
    tables.mower_health_daily.c.day == sa.bindparam("day", type_=sa.Date()),
)


def _latest(row: Any) -> LatestRow:
    return LatestRow(**dict(row._mapping))


class _SqlTx:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def latest(self, mower_id: UUID) -> LatestRow | None:
        row = (await self._conn.execute(_LATEST_ONE, {"mower_id": mower_id})).first()
        return _latest(row) if row else None

    async def put_latest(self, row: LatestRow) -> None:
        await self._conn.execute(_PUT_LATEST, _fields(row))

    async def add_history(self, row: HistoryRow) -> None:
        await self._conn.execute(_ADD_HISTORY, _fields(row))

    async def add_position(self, row: PositionRow) -> None:
        await self._conn.execute(_ADD_POSITION, _fields(row))

    async def add_command_event(self, event: CommandEvent) -> None:
        await self._conn.execute(_ADD_COMMAND_EVENT, _fields(event))

    async def put_snapshot(self, row: SnapshotRow) -> None:
        await self._conn.execute(_PUT_SNAPSHOT, _fields(row))

    async def directory_account(self, mower_id: UUID) -> UUID | None:
        result = await self._conn.execute(_DIRECTORY_ACCOUNT, {"mower_id": mower_id})
        account: UUID | None = result.scalar_one_or_none()
        return account

    async def ingest_rows(self, mower_ids: Collection[UUID]) -> IngestRows:
        ids = sorted(set(mower_ids))
        if not ids:
            return IngestRows(owners={}, latest={}, health={})
        params = {"ids": ids}
        owners = await self._conn.execute(_OWNERS, params)
        latest = await self._conn.execute(_LATEST_LOCKED, params)
        health = await self._conn.execute(_HEALTH_LOCKED, params)
        return IngestRows(
            owners=dict(owners.all()),
            latest={row.mower_id: LatestRow(**row._mapping, state={}) for row in latest.all()},
            health={row.mower_id: HealthRow(**dict(row._mapping)) for row in health.all()},
        )

    async def write_states(self, writes: StateWrites) -> None:
        """One statement per table, many rows each (``executemany``)."""
        batches: tuple[tuple[Any, list[Any]], ...] = (
            (_ADD_HISTORY, writes.history),
            (_ADD_POSITION, writes.positions),
            (_PUT_LATEST, list(writes.latest.values())),
            (_PUT_HEALTH, list(writes.health.values())),
            (_PUT_DAILY, list(writes.daily.values())),
        )
        for statement, rows in batches:
            if rows:
                await self._conn.execute(statement, [_fields(row) for row in rows])

    async def copy_ingested(
        self, source: UUID, target: UUID, account_id: UUID, received_at: datetime
    ) -> int:
        """One ``INSERT ... SELECT`` per table, ``ON CONFLICT DO NOTHING``."""
        replaced: dict[str, sa.ColumnElement[Any]] = {
            "mower_id": sa.literal(target, sa.Uuid()),
            "account_id": sa.literal(account_id, sa.Uuid()),
            "received_at": sa.literal(received_at, sa.DateTime(timezone=True)),
            "updated_at": sa.literal(received_at, sa.DateTime(timezone=True)),
        }
        copied = 0
        for table in _COPIED_TABLES:
            columns: list[sa.ColumnElement[Any]] = []
            for column in table.c:
                if column.name == "state":  # the latest row's MowerState names its mower
                    columns.append(_with_ids(column, target, account_id))
                elif column.name in replaced:
                    columns.append(replaced[column.name])
                else:
                    columns.append(column)
            names = [column.name for column in table.c]
            select = sa.select(*columns).where(table.c.mower_id == source)
            result = await self._conn.execute(
                pg_insert(table).from_select(names, select).on_conflict_do_nothing()
            )
            copied += result.rowcount
        return copied

    async def put_directory(self, entry: DirectoryEntry, *, replace: bool) -> None:
        insert = pg_insert(tables.mower_directory).values(**_fields(entry))
        if replace:
            statement = insert.on_conflict_do_update(
                index_elements=["mower_id"],
                set_={
                    "account_id": insert.excluded.account_id,
                    "updated_at": insert.excluded.updated_at,
                    "name": insert.excluded.name,
                    "serial": insert.excluded.serial,
                },
            )
        else:
            directory = tables.mower_directory.c
            statement = insert.on_conflict_do_update(
                index_elements=["mower_id"],
                set_={
                    "name": sa.func.coalesce(directory.name, insert.excluded.name),
                    "serial": sa.func.coalesce(directory.serial, insert.excluded.serial),
                },
            )
        await self._conn.execute(statement)

    async def drop_mower(self, mower_id: UUID) -> None:
        for table in (
            tables.mower_directory,
            tables.mower_latest,
            tables.mower_history,
            tables.mower_positions,
            tables.mower_positions_thinning,
            tables.command_timeline,
            tables.mower_snapshots,
            tables.mower_health,
            tables.mower_health_daily,
            tables.blade_changes,
            tables.tracking_sessions,
            tables.zone_days,
            tables.zone_presence,
            tables.virtual_zones,
        ):
            await self._conn.execute(sa.delete(table).where(table.c.mower_id == mower_id))

    async def health(self, mower_id: UUID) -> HealthRow | None:
        row = (await self._conn.execute(_HEALTH_ONE, {"mower_id": mower_id})).first()
        return HealthRow(**dict(row._mapping)) if row else None

    async def put_health(self, row: HealthRow) -> None:
        await self._conn.execute(_PUT_HEALTH, _fields(row))

    async def daily(self, mower_id: UUID, day: date) -> DailyRow | None:
        row = (await self._conn.execute(_DAILY, {"mower_id": mower_id, "day": day})).first()
        return DailyRow(**dict(row._mapping)) if row else None

    async def put_daily(self, row: DailyRow) -> None:
        await self._conn.execute(_PUT_DAILY, _fields(row))

    async def add_blade_change(self, change: BladeChange) -> None:
        await self._conn.execute(_ADD_BLADE_CHANGE, _fields(change))


def _fields(row: Any) -> dict[str, Any]:
    return {f.name: getattr(row, f.name) for f in dataclasses.fields(row)}


class _SqlRetention:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def delete_before(
        self, target: RetentionTarget, cutoff: datetime, batch_size: int
    ) -> int:
        column = _TIME_COLUMN[target]
        # Names come from the enums above, never from input; the cutoff is a bind parameter.
        statement = sa.text(
            f"DELETE FROM {target.value} WHERE ctid = ANY(ARRAY("  # noqa: S608
            f"SELECT ctid FROM {target.value} WHERE {column} < :cutoff LIMIT :batch))"
        ).bindparams(
            sa.bindparam("cutoff", type_=sa.DateTime(timezone=True)),
            sa.bindparam("batch", type_=sa.Integer()),
        )
        total = 0
        while True:
            result = await self._conn.execute(statement, {"cutoff": cutoff, "batch": batch_size})
            total += result.rowcount
            if result.rowcount < batch_size:
                return total

    async def thin_candidates(self) -> list[UUID]:
        result = await self._conn.execute(sa.select(tables.mower_directory.c.mower_id))
        return [row[0] for row in result.all()]

    async def thin_start(self, mower_id: UUID) -> datetime | None:
        mark = tables.mower_positions_thinning.c
        result = await self._conn.execute(
            sa.select(mark.thinned_until).where(mark.mower_id == mower_id)
        )
        stored: datetime | None = result.scalar_one_or_none()
        if stored is not None:
            return stored
        positions = tables.mower_positions.c
        oldest = await self._conn.execute(
            sa.select(sa.func.min(positions.observed_at)).where(positions.mower_id == mower_id)
        )
        found: datetime | None = oldest.scalar_one_or_none()
        return found

    async def thin_slice(
        self, mower_id: UUID, start: datetime, end: datetime, policy: ThinPolicy
    ) -> ThinResult:
        result = await self._conn.execute(
            _THIN_SLICE,
            {
                "mower_id": mower_id,
                "start": start,
                "end": end,
                "ctx_start": start - CONTEXT,
                "ctx_end": end + CONTEXT,
                "gap": timedelta(seconds=policy.gap_seconds),
                "active_s": policy.active_seconds,
                "idle_s": policy.idle_seconds,
                "mpd": METRES_PER_DEGREE,
                "moving": MOVING_METRES,
                "turn_min": TURN_MIN_STEP_M,
                "turn_cos": TURN_COS,
            },
        )
        deleted, scanned = result.one()
        mark = pg_insert(tables.mower_positions_thinning).values(
            mower_id=mower_id, thinned_until=end
        )
        await self._conn.execute(
            mark.on_conflict_do_update(
                index_elements=["mower_id"],
                set_={"thinned_until": mark.excluded.thinned_until},
                where=tables.mower_positions_thinning.c.thinned_until < mark.excluded.thinned_until,
            )
        )
        return ThinResult(scanned=scanned, deleted=deleted)


class SqlStore:
    """Implements :class:`~telemetry.application.ports.Store` on PostgreSQL."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Tx]:
        """``BEGIN`` ... ``COMMIT`` (``ROLLBACK`` on error)."""
        async with self.engine.begin() as conn:
            yield _SqlTx(conn)

    async def directory_of(self, mower_id: UUID) -> DirectoryEntry | None:
        async with read_connection(self.engine) as conn:
            result = await conn.execute(
                sa.select(tables.mower_directory).where(
                    tables.mower_directory.c.mower_id == mower_id
                )
            )
            row = result.first()
        return DirectoryEntry(**row._mapping) if row else None

    async def latest_of(self, account_id: UUID, mower_id: UUID) -> LatestRow | None:
        async with read_connection(self.engine) as conn:
            result = await conn.execute(
                sa.select(tables.mower_latest).where(
                    tables.mower_latest.c.account_id == account_id,
                    tables.mower_latest.c.mower_id == mower_id,
                )
            )
            row = result.first()
        return _latest(row) if row else None

    async def latest_many(
        self, account_id: UUID, mower_ids: Collection[UUID] | None, limit: int
    ) -> list[LatestRow]:
        if mower_ids is not None and not mower_ids:
            return []
        statement = (
            sa.select(tables.mower_latest)
            .where(tables.mower_latest.c.account_id == account_id)
            .order_by(tables.mower_latest.c.mower_id)
            .limit(limit)
        )
        if mower_ids is not None:
            statement = statement.where(tables.mower_latest.c.mower_id.in_(list(mower_ids)))
        async with read_connection(self.engine) as conn:
            result = await conn.execute(statement)
            return [_latest(row) for row in result.all()]

    async def history(
        self,
        account_id: UUID,
        mower_id: UUID,
        start: datetime,
        end: datetime,
        step: timedelta | None,
        limit: int,
    ) -> list[HistoryPoint]:
        async with read_connection(self.engine) as conn:
            if step is not None:
                result = await conn.execute(
                    _BINNED_HISTORY,
                    {
                        "step": step,
                        "account_id": account_id,
                        "mower_id": mower_id,
                        "start": start,
                        "end": end,
                        "limit": limit,
                    },
                )
                return [HistoryPoint(**dict(row._mapping)) for row in result.all()]
            h = tables.mower_history.c
            result = await conn.execute(
                sa.select(tables.mower_history)
                .where(
                    h.account_id == account_id,
                    h.mower_id == mower_id,
                    h.observed_at >= start,
                    h.observed_at < end,
                )
                .order_by(h.observed_at)
                .limit(limit)
            )
            return [
                HistoryPoint(
                    at=r.observed_at,
                    activity=r.activity,
                    battery_percent=r.battery_percent,
                    battery_min=r.battery_percent,
                    battery_max=r.battery_percent,
                    charging=r.charging,
                    rtk_fix=r.rtk_fix,
                    error_code=r.error_code,
                    samples=1,
                )
                for r in result.all()
            ]

    async def positions(
        self, account_id: UUID, mower_id: UUID, start: datetime, end: datetime, limit: int
    ) -> list[PositionRow]:
        p = tables.mower_positions.c
        async with read_connection(self.engine) as conn:
            result = await conn.execute(
                sa.select(*_POSITION_COLUMNS)
                .where(
                    p.account_id == account_id,
                    p.mower_id == mower_id,
                    p.observed_at >= start,
                    p.observed_at < end,
                )
                .order_by(p.observed_at.desc())
                .limit(limit)
            )
            # Positional construction: a track has up to tens of thousands of rows, and a dict
            # per row (``row._mapping``) was the largest single cost of the mower page.
            rows = [PositionRow(*row) for row in result.all()]
        rows.reverse()
        return rows

    async def position_chunks(
        self,
        account_id: UUID,
        mower_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        chunk_size: int,
    ) -> AsyncGenerator[list[PositionRow]]:
        """One query (index ``ix_mower_positions_mower_time``) streamed with a server side
        cursor, ``chunk_size`` rows at a time."""
        p = tables.mower_positions.c
        statement = (
            sa.select(tables.mower_positions)
            .where(
                p.account_id == account_id,
                p.mower_id == mower_id,
                p.observed_at >= start,
                p.observed_at < end,
            )
            .order_by(p.observed_at.desc())
            .limit(limit)
            .execution_options(yield_per=chunk_size)
        )
        async with self.engine.connect() as conn:
            result = await conn.stream(statement)
            async for part in result.partitions(chunk_size):
                yield [PositionRow(**dict(row._mapping)) for row in part]

    async def commands(self, account_id: UUID, mower_id: UUID, limit: int) -> list[CommandEvent]:
        c = tables.command_timeline.c
        async with read_connection(self.engine) as conn:
            result = await conn.execute(
                sa.select(tables.command_timeline)
                .where(c.account_id == account_id, c.mower_id == mower_id)
                .order_by(c.at.desc(), c.command_id.desc())
                .limit(limit)
            )
            return [CommandEvent(**dict(row._mapping)) for row in result.all()]

    async def snapshot_of(self, account_id: UUID, mower_id: UUID, kind: str) -> SnapshotRow | None:
        s = tables.mower_snapshots.c
        async with read_connection(self.engine) as conn:
            result = await conn.execute(
                sa.select(tables.mower_snapshots).where(
                    s.account_id == account_id, s.mower_id == mower_id, s.kind == kind
                )
            )
            row = result.first()
        return SnapshotRow(**dict(row._mapping)) if row else None

    async def snapshots_of_kind(
        self, account_id: UUID, mower_ids: Collection[UUID] | None, kind: str, limit: int
    ) -> list[SnapshotRow]:
        if mower_ids is not None and not mower_ids:
            return []
        s = tables.mower_snapshots.c
        statement = (
            sa.select(tables.mower_snapshots)
            .where(s.account_id == account_id, s.kind == kind)
            .order_by(s.mower_id)
            .limit(limit)
        )
        if mower_ids is not None:
            statement = statement.where(s.mower_id.in_(list(mower_ids)))
        async with read_connection(self.engine) as conn:
            result = await conn.execute(statement)
            return [SnapshotRow(**dict(row._mapping)) for row in result.all()]

    async def health_of(self, account_id: UUID, mower_id: UUID) -> HealthRow | None:
        h = tables.mower_health.c
        async with read_connection(self.engine) as conn:
            result = await conn.execute(
                sa.select(tables.mower_health).where(
                    h.account_id == account_id, h.mower_id == mower_id
                )
            )
            row = result.first()
        return HealthRow(**dict(row._mapping)) if row else None

    async def daily_of(
        self, account_id: UUID, mower_id: UUID, first: date, last: date
    ) -> list[DailyRow]:
        d = tables.mower_health_daily.c
        async with read_connection(self.engine) as conn:
            result = await conn.execute(
                sa.select(tables.mower_health_daily)
                .where(
                    d.account_id == account_id,
                    d.mower_id == mower_id,
                    d.day >= first,
                    d.day <= last,
                )
                .order_by(d.day)
            )
            return [DailyRow(**dict(row._mapping)) for row in result.all()]

    async def daily_many(
        self,
        account_id: UUID,
        mower_ids: Collection[UUID] | None,
        first: date,
        last: date,
        limit: int,
    ) -> list[DailyRow]:
        if mower_ids is not None and not mower_ids:
            return []
        d = tables.mower_health_daily.c
        statement = (
            sa.select(tables.mower_health_daily)
            .where(d.account_id == account_id, d.day >= first, d.day <= last)
            .order_by(d.mower_id, d.day)
            .limit(limit)
        )
        if mower_ids is not None:
            statement = statement.where(d.mower_id.in_(list(mower_ids)))
        async with read_connection(self.engine) as conn:
            result = await conn.execute(statement)
            return [DailyRow(**dict(row._mapping)) for row in result.all()]

    async def blade_changes(
        self, account_id: UUID, mower_id: UUID, limit: int
    ) -> list[BladeChange]:
        b = tables.blade_changes.c
        async with read_connection(self.engine) as conn:
            result = await conn.execute(
                sa.select(tables.blade_changes)
                .where(b.account_id == account_id, b.mower_id == mower_id)
                .order_by(b.changed_at.desc(), b.id.desc())
                .limit(limit)
            )
            return [BladeChange(**dict(row._mapping)) for row in result.all()]

    async def purge_account(self, account_id: UUID) -> int:
        """One transaction, one set based ``DELETE`` per table. The thinning watermark has no
        account column: it goes for the account's mowers (directory, latest row, positions)
        unless the directory gives the mower to another account."""
        directory = tables.mower_directory.c
        mine = sa.union(
            sa.select(directory.mower_id).where(directory.account_id == account_id),
            sa.select(tables.mower_latest.c.mower_id).where(
                tables.mower_latest.c.account_id == account_id
            ),
            sa.select(tables.mower_positions.c.mower_id).where(
                tables.mower_positions.c.account_id == account_id
            ),
        ).subquery()
        elsewhere = sa.select(directory.mower_id).where(directory.account_id != account_id)
        mark = tables.mower_positions_thinning.c
        removed = 0
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.delete(tables.mower_positions_thinning).where(
                    mark.mower_id.in_(sa.select(mine.c.mower_id)),
                    mark.mower_id.not_in(elsewhere),
                )
            )
            removed += result.rowcount
            for table in _ACCOUNT_TABLES:
                result = await conn.execute(
                    sa.delete(table).where(table.c.account_id == account_id)
                )
                removed += result.rowcount
        return removed

    @asynccontextmanager
    async def retention(self) -> AsyncIterator[RetentionScope | None]:
        """Session level ``pg_try_advisory_lock`` on a dedicated autocommit connection."""
        conn = await self.engine.connect()
        locked = False
        try:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            result = await conn.execute(
                sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": RETENTION_LOCK_KEY}
            )
            locked = bool(result.scalar_one())
            yield _SqlRetention(conn) if locked else None
        finally:
            try:
                if locked:
                    await conn.execute(
                        sa.text("SELECT pg_advisory_unlock(:key)"), {"key": RETENTION_LOCK_KEY}
                    )
            except Exception:
                _log.warning("releasing the retention lock failed, dropping the connection")
                await conn.invalidate()
            finally:
                await conn.close()
