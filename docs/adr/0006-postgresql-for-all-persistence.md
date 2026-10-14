# ADR 0006: PostgreSQL for all persistence, no SQLite

## Status

Accepted (2026-10-08)

## Context

Services need relational storage, and Telemetry needs time series (positions, RTK, battery). The earlier plan mentioned SQLite for the health history. Running two storage technologies complicates backups, migrations and the move from home to cloud.

## Decision

Use PostgreSQL 16 everywhere. In self-hosted mode there is one PostgreSQL instance with one database and one role per service; in the cloud it can be one instance per service or a managed cluster. No service reads the database of another service. Migrations use Alembic and run as a separate step before start. Time series start as plain tables; the TimescaleDB extension is an optional later optimization. SQLite is not used.

## Consequences

- One technology to back up, monitor and migrate.
- Moving from home to cloud is a database export.
- Telemetry volume must be watched; TimescaleDB or partitioning is the escape hatch.
- Higher baseline memory than SQLite, acceptable on a Pi 5 with 4 GB.
