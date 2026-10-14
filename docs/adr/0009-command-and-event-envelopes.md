# ADR 0009: Command and event envelopes, subject naming and de-duplication

## Status

Accepted (2026-10-08)

## Context

Services exchange mower commands, telemetry and domain events over NATS JetStream ([ADR 0007](0007-nats-jetstream-event-bus.md)). The plan requires that a command delivered twice is executed once, that a stale command (for example a "start" queued during an outage) is never executed, that an emergency stop always wins, and that contracts only evolve in a backward compatible way. ADR 0007 left the message format, the subject scheme and the enforcement of message ids and expiry to our contracts.

## Decision

All bus messages are wrapped in envelopes defined in `mowdeck-contracts` (`mowdeck_contracts.v1`):

- **CommandEnvelope**: `command_id` (UUID), `mower_id`, `account_id`, `issued_by` (principal reference), `issued_at`, `expires_at`, `priority` (`normal` or `emergency`), `correlation_id` and `command`, a discriminated union on `kind` (`start_mow`, `pause_mow`, `resume_mow`, `return_to_dock`, `emergency_stop`, `set_blade_height`, `set_speed`, `manual_drive`).
    - `expires_at` must be after `issued_at`. Consumers check `is_expired()` before execution and answer with a `CommandResult` of status `expired`; publishers refuse to send expired commands.
    - `priority` is `emergency` if and only if the command is `emergency_stop`, and is derived when omitted. The priority is also sent as the `Mowdeck-Priority` header so the Device-Gateway can fast-path it without decoding.
    - `manual_drive` carries its own dead-man timeout `ttl_ms` (100 to 2000 ms, default 1000).
- **CommandResult**: `command_id`, `mower_id`, `status` (`accepted`, `rejected`, `executed`, `expired`, `failed`), `reason`, `at`.
- **EventEnvelope[Payload]**: `id` (UUID), `type` (`<domain>.<name>`, e.g. `mower.registered`), `version` (integer, starting at 1), `occurred_at`, `account_id`, `correlation_id`, `causation_id`, `producer` and the typed `payload`.
- All timestamps are timezone-aware UTC. Models ignore unknown fields (tolerant reader).

**De-duplication** uses JetStream: the envelope id (`command_id` or event `id`) is sent as the `Nats-Msg-Id` header. JetStream drops a repeated id within the stream's duplicate window (120 s for commands and events, 30 s for telemetry). Consumers stay idempotent for redeliveries after a missing ack.

**Subject naming** (refines ADR 0007; events get an `event.` prefix so one stream captures all of them):

| Subject | Stream | Payload |
| --- | --- | --- |
| `command.mower.<mower_id>` | `COMMANDS` (`command.>`) | `CommandEnvelope` |
| `command.result.mower.<mower_id>` | `COMMANDS` | `CommandResult` |
| `telemetry.mower.<mower_id>` | `TELEMETRY` (`telemetry.>`) | `MowerState` |
| `event.<domain>.<name>` | `EVENTS` (`event.>`) | `EventEnvelope` |

Other subjects (for example `svc.<service>.<operation>` for request/reply) use core NATS and are not persisted. Stream limits are defined as data in `mowdeck_contracts.v1.subjects` and applied by `mowdeck-service-kit` on connect.

**Versioning**: within `v1` only backward compatible changes (new optional fields). An event is never reinterpreted; a changed meaning gets a new event `version`. An incompatible envelope change creates a new namespace `mowdeck_contracts.v2`. JSON Schemas are exported to `libs/mowdeck-contracts/schemas/v1/` and a test fails when they are out of date.

Options considered: CloudEvents as the envelope (rejected for now: more fields than needed and no typed command union; the envelope maps onto CloudEvents attributes if an external integration needs it), and de-duplication in each consumer with a database table (rejected: JetStream already provides it within a window, consumers only need idempotency for redeliveries).

## Consequences

- Duplicate publishes within the window are harmless; stale and duplicated commands cannot move a mower.
- Emergency stops are recognisable from the header alone and cannot be downgraded by a client.
- Every producer and consumer depends on `mowdeck-contracts`; changing a model requires regenerating and committing the schemas.
- De-duplication only covers the duplicate window. Longer-lived idempotency (for example for scheduled commands) must be handled by the producer, which issues a new `command_id` per execution.
- Telemetry and command results are bounded by stream limits; history lives in the Telemetry service and the audit log.
