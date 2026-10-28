# Telemetry: retention and position downsampling

Telemetry bounds its tables with age limits and thins old positions at night ([ADR 0053](../adr/0053-position-retention-and-downsampling.md), [ADR 0017](../adr/0017-telemetry-storage-and-retention.md)). The job runs inside the Telemetry service (one replica at a time, advisory lock); there is nothing to schedule outside.

## Settings

Set as environment variables of the Telemetry service (Compose `.env`, Helm `telemetry.env`). Compose and the Helm chart pass `TELEMETRY_RETENTION_POSITIONS_DAYS`, `TELEMETRY_POSITIONS_DETAIL_DAYS` and `TELEMETRY_MAINTENANCE_TIMEZONE` through under the `MOWDECK_` names below.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOWDECK_TELEMETRY_RETENTION_HISTORY_DAYS` | `30` | History rows older than this are deleted |
| `MOWDECK_TELEMETRY_RETENTION_COMMANDS_DAYS` | `30` | Command timeline rows older than this are deleted |
| `MOWDECK_TELEMETRY_RETENTION_POSITIONS_DAYS` | `730` | Positions older than this are deleted; `0` keeps them forever; otherwise at least the detail days |
| `MOWDECK_TELEMETRY_POSITIONS_DETAIL_DAYS` | `90` | Positions younger than this keep every row (at least 7) |
| `MOWDECK_TELEMETRY_POSITIONS_THINNING_ENABLED` | `true` | Thin older positions; `false` only deletes |
| `MOWDECK_TELEMETRY_POSITIONS_THIN_ACTIVE_SECONDS` | `30` | One row per this many seconds while mowing or moving (divides 300) |
| `MOWDECK_TELEMETRY_POSITIONS_THIN_IDLE_SECONDS` | `300` | One row per this many seconds while standing (divides 300) |
| `MOWDECK_TELEMETRY_MAINTENANCE_TIMEZONE` | `UTC` | IANA zone of the window, for example `Europe/Berlin` |
| `MOWDECK_TELEMETRY_THINNING_WINDOW_START_HOUR` | `1` | Local hour the nightly window opens |
| `MOWDECK_TELEMETRY_THINNING_WINDOW_END_HOUR` | `5` | Local hour it closes (exclusive); equal to the start means always |
| `MOWDECK_TELEMETRY_THINNING_BUDGET_SECONDS` | `900` | Longest one round runs |
| `MOWDECK_TELEMETRY_RETENTION_INTERVAL_SECONDS` | `3600` | How often the job wakes up |

Example for a German installation: `MOWDECK_TELEMETRY_MAINTENANCE_TIMEZONE=Europe/Berlin`. The window follows daylight saving time on its own.

## What to expect

- The first nights after an upgrade work through the backlog (one SQL statement per 6 hours of one mower, at most 15 minutes per round, rounds hourly inside the window). Watch `mowdeck_telemetry_position_thinning_rows_deleted_total` and the log line `position thinning round` (`complete=false` means more to do tomorrow).
- Metrics: `mowdeck_telemetry_retention_rows_deleted_total{table}`, `mowdeck_telemetry_position_thinning_rows_scanned_total`, `mowdeck_telemetry_position_thinning_rows_deleted_total`, `mowdeck_telemetry_retention_round_seconds{kind}`.
- Disk space is reused by PostgreSQL after autovacuum; the files shrink only with a rewrite. After the first large run, a one-time `VACUUM (ANALYZE) mower_positions;` is fine and lets the planner see the new sizes. Do not run `VACUUM FULL` on a live database (it locks the table).
- Heatmaps and tracks older than the detail window show fewer points; coverage is joined across the gaps.

## Check sizes and the expected saving (read only)

Run with `psql` against the Telemetry database. Nothing here writes.

```sql
-- 1. Size of the Telemetry tables (heap + indexes) and the estimated row count.
SELECT c.relname AS table_name,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total,
       pg_size_pretty(pg_relation_size(c.oid)) AS heap,
       c.reltuples::bigint AS approx_rows
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind = 'r'
   AND c.relname IN ('mower_positions', 'mower_history', 'command_timeline',
                     'mower_snapshots', 'mower_health_daily', 'mower_positions_thinning')
 ORDER BY pg_total_relation_size(c.oid) DESC;

-- 2. Positions per mower: count, oldest, newest, rows older than the detail window.
SELECT mower_id,
       count(*) AS rows,
       min(observed_at) AS oldest,
       max(observed_at) AS newest,
       count(*) FILTER (WHERE observed_at < now() - interval '90 days') AS older_than_90d
  FROM mower_positions
 GROUP BY mower_id;

-- 3. Expected saving of the default policy on rows older than 90 days: rows now versus
--    roughly the rows that survive (one per 30 s bin while blades_on, one per 5 min bin
--    otherwise; edges and turns add a little on top).
WITH old AS (
  SELECT mower_id, observed_at, coalesce(blades_on, false) AS active
    FROM mower_positions
   WHERE observed_at < now() - interval '90 days'
)
SELECT (SELECT count(*) FROM old) AS rows_now,
       (SELECT count(DISTINCT (mower_id,
                date_bin(CASE WHEN active THEN interval '30 seconds' ELSE interval '5 minutes' END,
                         observed_at, TIMESTAMPTZ '2000-01-01 00:00:00+00'), active))
          FROM old) AS rows_after_approx;

-- 4. Progress of the thinning: the watermark per mower.
SELECT mower_id, thinned_until FROM mower_positions_thinning ORDER BY thinned_until;
```

Rule of thumb: multiply the bytes of query 1 by the ratio `rows_after_approx / rows_now` of query 3 for the share of the table that is older than the detail window.
