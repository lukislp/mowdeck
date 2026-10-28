# ADR 0053: Retention and downsampling of the position track

## Status

Accepted (2026-10-27). Extends [ADR 0017](0017-telemetry-storage-and-retention.md).

## Context

`mower_positions` is the largest table of Telemetry: a row for every changed position of a moving mower (every 1 to 20 s), a row when the RTK fix changes, and since the signal heatmaps ([ADR 0017](0017-telemetry-storage-and-retention.md), migration 0007) also rows while the mower stands. ADR 0017 deleted everything older than 90 days, which bounds the table but throws away the history the heatmaps and the track are for: a season of coverage, the signal map over a year. Keeping everything grows without bound and slows the heatmap scans.

Readers of the table:

- the heatmaps (`GET /mowers/{id}/heatmap`, layers coverage, rtk, wifi, mnet; default the last 7 days, any range up to the query limit), which join consecutive blades-on positions into swaths when at most `MOWDECK_TELEMETRY_HEATMAP_JOIN_SECONDS` / `_METRES` apart;
- the position track (`GET /mowers/{id}/positions`, `tracks.positions.query`, the map trail and the coverage of spot mowing);
- the history chart (`date_bin` joins the last position of each bin).

Not readers: the health totals and `mower_health_daily` are computed on ingest and never read positions again; they are untouched. `mower_history` is already change-based (a row per change, one per five minutes for an unchanged state) and `mower_snapshots` holds one row per mower and kind; neither grows with the heartbeat rate.

## Decision

**Three age bands for positions.**

1. Younger than `MOWDECK_TELEMETRY_POSITIONS_DETAIL_DAYS` (default 90, at least 7): every row, never touched.
2. Older: downsampled by the rules below.
3. Older than `MOWDECK_TELEMETRY_RETENTION_POSITIONS_DAYS` (default 730, `0` keeps them forever, otherwise at least the detail days): deleted by the existing batch delete.

**What survives downsampling.** Per mower, a row is kept when it is

- the first row of its bin, with bins of `MOWDECK_TELEMETRY_POSITIONS_THIN_ACTIVE_SECONDS` (30) while the blades run or the mower moved more than 1 m since the previous row, and of `..._THIN_IDLE_SECONDS` (300) otherwise (both divide 300; bins are anchored at 2000-01-01 UTC like the history bins), or
- an edge: next to a gap of more than 120 s (a session start or end, a silence), next to a change of `blades_on` (job start and stop, docking, transfers) or of the RTK fix, or
- a turn: the track bends by more than 25 degrees at the row (steps under 0.3 m are noise), so lane ends and corners keep their shape, or
- observed at the same instant as a history row with an error code.

Everything else is deleted. The same rules exist twice, as a pure function (`telemetry.domain.thinning.keep_set`, used by the in-memory store) and as one SQL statement per slice; a test runs both on random tracks and compares the survivors.

**Heatmaps on thinned rows.** Kept rows of one swath are at most two active bins apart, which is more than the 30 s join limit. Rather than keeping blades-on detail longer, the heatmap joins a pair of points with the wider limits `max(join_seconds, 2 * active + 5)` (65 s) and the metres scaled by the same factor (about 22 m) when the older point is older than the detail window. Recent data is joined exactly as before. The straight joins are exact on straight lanes (the kept points are collinear) and the turn rule keeps the corners; the test compares the covered area before and after thinning (within 12 %) and shows that without the wider join it would fall apart into discs. The signal and RTK layers bin points independently and only see fewer, evenly spread samples (one per 30 s moving, one per 5 min standing), which the median and the fixed share tolerate.

**How it runs.** The maintenance loop already running the retention (`MOWDECK_TELEMETRY_RETENTION_INTERVAL_SECONDS`, hourly, advisory lock on one replica) calls `thin_once` after the deletes. It does nothing outside the nightly window `MOWDECK_TELEMETRY_THINNING_WINDOW_START_HOUR` to `..._END_HOUR` (default 1 to 5, local hour in `MOWDECK_TELEMETRY_MAINTENANCE_TIMEZONE`, default UTC; equal hours mean always; the window may wrap midnight). The window is evaluated on the local wall-clock hour of the aware timestamp, so DST needs no special case: a spring night has one real hour fewer inside a window that spans the jump, an autumn night one more, and a window inside the skipped hour is never entered; the work is resumable, so a short night only delays it. A round works slice by slice (at most 6 hours of one mower per statement, so a statement touches a few thousand rows), stops after `MOWDECK_TELEMETRY_THINNING_BUDGET_SECONDS` (900) or when the window closes, and releases the lock.

**Idempotent and safe with concurrent writes.** Table `mower_positions_thinning (mower_id, thinned_until)` (migration 0011) is the watermark: a slice runs once, then the watermark moves past it; a crash between the delete and the watermark repeats the slice, which deletes nothing more. Slice ends lie on a 5-minute grid so bins never straddle a boundary, and the statement reads 10 minutes of context on both sides to see the neighbours of its edge rows. Slices end at least the detail window in the past, ingest only inserts at the present, so the writer never conflicts with the deletes; the delete by `(mower_id, observed_at)` uses the primary key. Rows that arrive later behind the watermark (clock skew, a replay) are not thinned but are deleted by the age limit. No index is added: the slice scans use `ix_mower_positions_mower_time`, the age delete the BRIN index. The statements use no `VACUUM`; autovacuum reclaims the space for reuse, and a one-time `VACUUM (ANALYZE)` after the first big run is documented in the ops guide (never `VACUUM FULL`).

**Rule for other consumers of raw positions (theft tracking).** Anything that needs full-resolution rows must read only data younger than the detail window, or store its own copy. Thinning never touches a row younger than `POSITIONS_DETAIL_DAYS`, and that value is at least 7 days, so a tracking session shorter than the detail window cannot be thinned while it is read. A feature that flags a longer session must raise the detail window or copy the rows.

**Other tables.** `mower_history` (30 days) and `command_timeline` (30 days) keep their age limits; they are change-based and small. `mower_snapshots`, `mower_health`, `mower_health_daily` and `blade_changes` are one row per mower, kind or day and stay until the mower is removed.

**Metrics and logs.** `mowdeck_telemetry_retention_rows_deleted_total{table}`, `mowdeck_telemetry_position_thinning_rows_scanned_total`, `mowdeck_telemetry_position_thinning_rows_deleted_total` and the histogram `mowdeck_telemetry_retention_round_seconds{kind=delete|thin}`; one info line per round that did work (mowers, slices, scanned, deleted, seconds, complete).

## Consequences

- The default for positions changes from "delete after 90 days" to "thin after 90 days, delete after 730 days". Installations that want the old behaviour set `MOWDECK_TELEMETRY_RETENTION_POSITIONS_DAYS=90`.
- An old position track has gaps of up to 30 s while mowing (straight between kept points) and 5 minutes while standing; exact trails of old jobs are no longer available. Hot data, the live map and the last 90 days are unchanged.
- The first run after an upgrade walks every slice from the mower's oldest row to the cutoff, one statement per 6 hours; it takes several nights on a large table and shows up in the metrics. Later nights process one day.
- Row counts shrink roughly by the ratio of the stored interval to the bin width; the ops guide has the read-only SQL to measure it on a live database.
- Thinning is tuned by the settings; changing the bin widths affects only slices not yet processed.
