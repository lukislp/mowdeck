# ADR 0078: Batched state ingestion

## Status

Accepted (2026-11-02). Follow-up of [ADR 0076](0076-demo-sandboxes.md) (its measurements) and of the load test ([Load testing](../ops/load-testing.md), "Ingestion cost"). Changes how Telemetry and Notifications consume `telemetry.mower.*`; the write rules of [ADR 0017](0017-telemetry-storage-and-retention.md) and [ADR 0053](0053-position-retention-and-downsampling.md) stay as they are.

## Context

Every state a mower reports is consumed by Telemetry (latest row, history, positions, health totals, virtual zones), Notifications (the rules), the Scheduler (busy check), Vision (automatic analysis and its view), the camera relay and Realtime (the live fan-out). A simulated mower that mows reports once a second, a real one every few seconds while it works and with every heartbeat (ADR 0040) otherwise.

ADR 0076 measured that a demo sandbox watched by its visitor, one mower mowing, costs about 5 % of a core, and that most of it is not the simulator but the processing of its states. Profiling the consumers (py-spy, 20 watched sandboxes, about 27 states a second) showed where it went:

- **One transaction per state and consumer.** Telemetry ran about nine statements per state (directory, latest row `FOR UPDATE`, history, position, latest upsert, health `FOR UPDATE`, health upsert, day row, day upsert) plus the pool's ping, `BEGIN` and `COMMIT`; Notifications checked three connections out of the pool per state (directory, geofence, the rule transaction), each with a ping; the Scheduler one more transaction. Postgres committed about 80 transactions a second for 27 states.
- **Building SQL per message.** Every upsert built its statement anew (`insert(...).values(...)`, `on_conflict_do_update(set_={... excluded ...})`): SQLAlchemy recomputed the `excluded` column collection and the cache key each time. In Telemetry and the Scheduler this was the largest single cost, more than the database round trips.
- **Writes nobody needs.** The Scheduler upserted the mower's activity and position for every state; it needs the activity when it changes and the position for the weather.
- **Acknowledgements of live views.** Each state reached seven consumers, each acknowledged it: eight NATS messages per state, although three of them (Realtime's fan-out, Vision's and the camera relay's latest state) gain nothing from a redelivery.

## Decision

### Batch subscriptions in the service kit

`EventBus.subscribe_batch` (JetStream and in-memory) hands a handler all messages that arrived while its previous batch ran, up to `max_batch`, after lingering `max_wait_seconds` for more. The handler answers one outcome per message (acknowledge, redeliver, drop). Messages are settled only **after** the handler returned, so the delivery guarantee is unchanged: what was not acknowledged comes again (at least once). Batches of one subscription never overlap, so delivery order is kept. Raising from the handler redelivers the whole batch. Unsubscribing (shutdown) stops new deliveries and lets the worker handle and settle what it already received, within the drain timeout; anything left unsettled is redelivered by the server. JetStream's `max_ack_pending` (1000) bounds the worker's queue.

### Telemetry: one transaction per batch, the same rows

`Ingest.record_states` reads the directory accounts, the latest rows (without the stored state document) and the health totals of the batch's mowers once, locked in mower id order (two replicas never deadlock), applies the write rules **state by state in arrival order** to these rows in memory and writes one statement per table: every history and position row, and per mower the last latest row, the health totals and the day totals. The result is exactly what the same states stored one at a time would produce; tests compare both for the memory store and PostgreSQL. A state that names another account than the directory or the stored rows is left out and dropped alone; the others are stored. A database failure stores nothing and redelivers the batch; a redelivered state changes nothing (the existing redelivery rules: an equal `observed_at` writes no history or position, `advance` counts no interval twice). Blade threshold events are published and virtual zones evaluated after the commit, in order (the zones of all the batch's mowers read at once).

`MOWDECK_TELEMETRY_INGEST_BATCH_MAX` (64) and `MOWDECK_TELEMETRY_INGEST_BATCH_WAIT_SECONDS` (0.2) configure it; the stored latest state lags the stream by at most the wait, which no client notices (live states come over Realtime). The demo's history import stores 200 states per transaction through the same path.

### Notifications: one transaction per batch

`Watcher.on_states` reads each mower's directory account, geofence key and watch once per batch (one statement for account and geofence key, the watch `FOR UPDATE` in mower id order), runs the rules state by state, writes each changed watch once and fans the derived events out after the commit, in order: the existing guarantee (at most once per derived event, never twice for a redelivered state) stays. The parsed geofence is kept per mower and stored version (`observed_at`) instead of being read and parsed for every state. Settings `MOWDECK_NOTIFICATIONS_STATE_BATCH_MAX` and `_STATE_BATCH_WAIT_SECONDS`.

### The rest

- **Statements built once** (Telemetry, Notifications, Scheduler): the upserts on the ingestion paths are module level statements executed with a parameter dict.
- **The Scheduler writes the status when it matters**: an activity change at once, an unchanged activity at most every 30 s or after a move of 100 m. The follow-up of a started run and the weather fallback position tolerate that lag; an older report is never written (as before).
- **Live views without acknowledgements**: `subscribe(..., acknowledge=False)` creates a JetStream consumer with `AckPolicy.NONE`, refused for durable and queue consumers. Realtime's fan-out of states, Vision's view and the camera relay's latest state use it; every consumer that stores or acts keeps explicit acknowledgements.
- **Demo knob**: `MOWDECK_SIM_STATE_PERIOD_SECONDS` (Device-Gateway, simulated mowers only; demo `DEMO_SIM_STATE_PERIOD_SECONDS`, default 0 = unchanged) lets an operator report periodic states less often than once a second; changes are still reported at once.

## Measurements

See [Load testing](../ops/load-testing.md), "Ingestion cost" (method: `tools/repo-checks/scripts/demo_sandbox_cost.py`, alternating runs of `main` and this change on the same host).

## Consequences

- Fewer, larger transactions: Postgres commits per state drop by about five times in Telemetry and Notifications, the Scheduler writes about a tenth of what it wrote; the rows written per state stay the same (each state of a mowing mower is one more history or position row, one latest row and one health row: the stored data does not change).
- A batch holds its mowers' latest and watch rows locked a little longer; replicas lock in mower id order and cannot deadlock.
- Telemetry's stored latest state and Notifications' rules see a state up to the batch wait (0.2 s) later. `MOWDECK_..._BATCH_WAIT_SECONDS=0` batches only what queued up while the previous batch was written.
- Realtime, Vision's view and the camera relay no longer get a failed state again; the next state follows within seconds.
- New consumers of a high-rate subject should prefer a batch subscription when they write per message, and `acknowledge=False` when they only keep or forward the latest value.

## Alternatives considered

- **Coalescing per mower (write only the last state of a window)**: fewer rows, but history and position rows are the product (tracks, heatmaps, health); dropping intermediate states would change the data of real mowers. Batching keeps every row.
- **Skipping the latest-row update when nothing relevant changed**: the latest row carries `observed_at`, the heartbeat and the full state the API serves; skipping it would serve stale positions.
- **A memory-backed `TELEMETRY` stream**: cheaper for NATS, but states published while a consumer is down would not survive a NATS restart, and changing the storage of an existing stream needs it recreated. Not worth it next to the acknowledgement savings.
- **A lower state rate for real mowers**: the rate is the vendor's (and the adaptive interval of the Mammotion adapter); only the simulator's rate is MowDeck's, hence the demo knob.
