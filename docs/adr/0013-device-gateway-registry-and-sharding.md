# ADR 0013: Device-Gateway registry, sharding and per-mower concurrency

## Status

Accepted (2026-10-08)

## Context

The Device-Gateway is the only service that talks to mowers ([ADR 0012](0012-mower-control-interface-and-safety-rules.md)). The plan gives it only volatile data, scales it by the number of connected mowers with sharding by mower id, and requires that commands carry an id and an expiry, that an emergency stop has priority, and that no command is lost on shutdown. Fleet owns the mowers but does not exist yet; Phase 1 still has to run end to end with a simulated mower.

Open questions for the first implementation:

- Where does the gateway learn which mowers exist, with which account and which connection?
- How are mowers spread over several instances, and how does a command reach the right one?
- How are commands processed so that one slow mower cannot delay an emergency stop of another?
- When is a command acknowledged on the bus?

## Decision

**Volatile registry fed by Fleet.** The gateway stores nothing. On start it subscribes to the Fleet events `mower.registered`, `mower.updated` and `mower.removed` (payload `MowerRegistration` in an `EventEnvelope`) and then asks Fleet with a core NATS request on `fleet.mowers.list` (`MowerListRequest` with the shard, reply `MowerListReply`). While Fleet does not answer, the request is retried in the background with exponential backoff (1 s up to 30 s) and logged as a warning; the gateway keeps running and is ready with the mowers it knows. For development, `MOWDECK_BOOTSTRAP_MOWERS` (a JSON list of `MowerRegistration`) seeds the registry; it is documented as development only. Registrations are applied additively; `mower.updated` renames in place, and a changed connection kind or option reconnects the mower. The contracts live in `mowdeck_contracts.v1.fleet`.

**Static modulo sharding.** A mower belongs to instance `i` of `n` when `mower_id.int % n == i` (`MOWDECK_SHARD_INDEX`, `MOWDECK_SHARD_COUNT`). Every instance has its own durable JetStream consumer `device-gateway-<i>` on `command.mower.*`, receives every command and acknowledges the commands of other shards without acting. There is no queue group across shards: a queue group would hand a command to an arbitrary instance, which may not hold the connection.

**Per-mower concurrency.** Every admitted command runs in its own task on the mower's `MowerController`. The controller serializes ordinary commands of one mower in arrival order and lets an emergency stop overtake them (ADR 0012), so the gateway adds no queue of its own: commands of different mowers never wait for each other, and an emergency stop does not wait for a slow command of the same mower either.

**Admission before execution.** The handler validates the envelope (invalid or subject mismatch: terminated), ignores foreign shards, rejects unknown mowers and envelopes whose `account_id` differs from the registered owner (tenancy defence in depth, the command never reaches the controller) and hands the rest to the session. The message is acknowledged once admitted; the result follows on `command.result.mower.<id>` with the msg id `<command_id>:<status>`, so a redelivered command (the controller de-duplicates by command id) yields one stored result.

**Graceful shutdown.** Readiness turns 503, the consumer is unsubscribed (deliveries arriving meanwhile are negatively acknowledged and redelivered after the restart), the gateway waits until every admitted command has published its result, closes every controller (ending drive sessions, publishing `mower.disconnected`), and only then does the kit drain the bus.

**Telemetry.** Every state change from the controller (`watch_state()`) is published on `telemetry.mower.<id>` when anything except `observed_at` and the adapter specific `raw` field changed; the latest state is republished as a heartbeat every `MOWDECK_TELEMETRY_HEARTBEAT_SECONDS` (default 10). Links are reported as `mower.connected` and `mower.disconnected`.

Alternatives considered:

1. *Persist the registry in the gateway (own database).* Rejected: duplicates Fleet's data, needs synchronisation, and the plan restricts the gateway to volatile data.
2. *Shared queue group or one shared durable for all instances.* Rejected: commands would land on instances without the connection. Per-instance subjects (`command.mower.<shard>.<id>`) would avoid the filtering but put the shard count into the subject scheme and into every producer.
3. *Consistent hashing or a lease-based assignment (NATS KV).* Better for re-sharding without moving most mowers, but needs coordination and hand-over logic that Phase 1 does not need. Kept as the upgrade path.
4. *One worker task and queue per mower in the gateway.* Rejected: it would serialize an emergency stop behind a slow command of the same mower, duplicating and weakening the controller's own priority rule.
5. *Acknowledge after the result is published.* Rejected for now: the kit acknowledges when the handler returns and push consumers deliver one message at a time per subscription, so waiting for execution would serialize all mowers. Admitted commands are instead protected by the shutdown drain; a crash can lose an admitted command, which then expires on the caller's side.
6. *Fail start-up without Fleet.* Rejected: it couples the start order of services and breaks the "each service can fail on its own" rule.

## Consequences

- Fleet must answer `fleet.mowers.list` and publish the registration events with `MowerRegistration` payloads; the contracts are defined now, ahead of Fleet.
- Every instance receives every command; the filtering cost is one modulo per command, fine for the expected fleet sizes. Changing `MOWDECK_SHARD_COUNT` moves mowers between instances without hand-over (they reconnect on the new owner; commands during the switch may be rejected or fail). Kubernetes runs the gateway as a StatefulSet with the index from the pod ordinal.
- A crash between acknowledgement and result loses that command's result; callers rely on the expiry and the telemetry. Revisit when the kit supports acknowledging after completion.
- `MOWDECK_BOOTSTRAP_MOWERS` must be removed from deployments once Fleet exists.
- Connection options are passed to adapters unchanged and must not contain secrets; real adapters will reference credentials instead.
