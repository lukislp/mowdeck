# ADR 0007: NATS JetStream as event bus

## Status

Accepted (2026-10-08)

## Context

Commands, telemetry and domain events must flow asynchronously between services so that any service can fail or scale without taking others down. Commands need deduplication and expiry; telemetry needs fan-out to Realtime and Telemetry.

## Decision

Use NATS with JetStream. Subjects: `command.mower.<id>`, `telemetry.mower.<id>` and domain events such as `mower.registered` and `subscription.changed`. Event schemas are defined in `mowdeck-contracts` (JSON Schema and AsyncAPI). A single container runs at home; a NATS cluster runs in the cloud.

## Consequences

- Light single binary that fits a Raspberry Pi.
- Persistent streams and replay via JetStream.
- Message IDs and expiry timestamps are enforced by our contract, not by NATS.
- The team needs to learn JetStream consumers and retention settings.
