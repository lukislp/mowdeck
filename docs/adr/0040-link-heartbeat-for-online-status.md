# ADR 0040: Link heartbeat for online status

## Status

Accepted (2026-10-15)

## Context

The web marked a mower offline when `MowerState.observed_at` was older than 45 s. `observed_at` is when the mower reported its values, and the Device-Gateway's heartbeat (every `MOWDECK_TELEMETRY_HEARTBEAT_SECONDS`, default 10 s, [ADR 0013](0013-device-gateway-registry-and-sharding.md)) repeats the newest state with that same time. A docked mower legitimately reports rarely: a cloud mower may stay silent for minutes while the gateway's connection to it is perfectly alive. Such a mower showed dashed "offline" badges, hollow map markers and a disabled firmware update, although the gateway held the link and commands would have worked.

The question a client actually asks is whether the gateway holds a live connection to the mower now. Only the gateway knows: it owns the connection (`MowerConnection.is_connected`) and watches it. The connection events `mower.connected` and `mower.disconnected` say it too, but they travel on the EVENTS stream, which neither Realtime nor the web reads, and they carry no values. The activity `offline` exists, but it is what an adapter maps the mower's own report to (sleeping, powered off), and Notifications turn it into an immediate offline notification without the disconnect grace time.

## Decision

**Two gateway fields on `MowerState`** (`mowdeck_contracts.v1`):

- `heartbeat_at: datetime | None`: when the gateway last knew its connection to the mower alive. It is set on every publish; null only in states from before the field.
- `connected: bool = true`: false in the one state the gateway publishes right after the connection ended.

Both are set by the Device-Gateway's session and never by an adapter: the session overwrites them on every publish, and the gateway's change detection (`is_significant_change`) ignores them like `observed_at`, because they describe the link, not the mower's report.

**The gateway's link heartbeat.** The existing heartbeat becomes the link heartbeat: while the run loop is up, the latest state is republished every `MOWDECK_TELEMETRY_HEARTBEAT_SECONDS` (default 10 s; 1 s with an open drive session) even when the adapter reported nothing new, with a fresh `heartbeat_at` and the unchanged `observed_at`. A heartbeat goes out only while `MowerConnection.is_connected` is true, so a link that dropped between two checks of the link watch publishes no further heartbeats. A stream of insignificant reports no longer postpones the heartbeat either: once it is due, the newest report goes out. When the link ends (lost, failed or released), the session publishes the last state once more with `connected = false` and the last `heartbeat_at` before it closes the connection, then the `mower.disconnected` event as before. The simulator path is the same code.

**Consumers.**

- Telemetry stores every state in `mower_latest` (its upsert already accepts the same `observed_at`). A state with the same `observed_at` as the stored one, which is a heartbeat, the offline state or a redelivery, writes neither history nor position rows; the periodic rows (one history row per `MOWDECK_TELEMETRY_HEARTBEAT_HISTORY_SECONDS`, the position rows of a standing mower for the signal heatmap) keep following the mower's own reports. The REST `stale` flag means offline: `connected` false, or the last heartbeat (`observed_at` as fallback) older than `MOWDECK_TELEMETRY_STALE_AFTER_SECONDS`.
- Realtime passes the fields through unchanged.
- Notifications ignore the offline state: it proves no contact, and `mower.disconnected` drives the offline timer with its grace time.
- The web treats a mower as online while `connected` is not false and the last heartbeat is at most 45 s old (four missed heartbeats); states without `heartbeat_at` fall back to `observed_at`. The age of the values is a separate, subdued line ("Werte von vor 4 min") shown only while the mower is online and its values are older than three minutes. Its store keeps the newest report and, for the same report, the newest heartbeat.

**Options considered.** Reusing the activity `offline` for the disconnect state would lose the last activity, write a history row per disconnect and fire immediate notifications. Reading the connection events in Realtime and the web would add a second channel that has to be merged with the states and still leaves no liveness signal between events. Stamping `observed_at` with the publish time would make the values look fresh and break the history and health rules that rely on it.

## Manufacturer independence

The rule lives in the gateway's session and in the neutral contract. Liveness comes from the generic `MowerConnection.is_connected`, which every adapter implements; nothing branches on a manufacturer, a model or a connection kind. Adapters keep reporting `observed_at` as the time of the mower's report, however often their vendor sends one, and have no say in `heartbeat_at` or `connected`. A new adapter therefore gets correct online and offline display without any code of its own, and a vendor whose mower reports rarely (a docked LUBA on the cloud) looks the same as one that streams.

## Consequences

Easier: a docked, connected mower shows online; a dropped link shows offline at once instead of after 45 s; the web and the REST `stale` flag use one rule, which any API client can apply from the state alone. Harder: two times on a state to explain (`observed_at` for the values, `heartbeat_at` for the link); the heartbeat now rewrites the `mower_latest` row every 10 s per connected mower, which it did already. Follow-ups: none required; the mobile shell uses the web app and follows automatically.
