# ADR 0004: Monorepo with a uv workspace

## Status

Accepted (2026-10-08)

## Context

The platform consists of many small services and libraries that evolve together and share contracts. Maintaining many repositories would multiply version and CI overhead.

## Decision

Use one repository with a uv workspace (`libs/*`, `services/*`, `tools/*`) and one lockfile. Each service and library is its own Python package with its own `pyproject.toml`, README and tests, and each service builds into a separate image. Tooling is shared: Ruff, mypy (strict), pytest, Poe the Poet tasks, MkDocs. One CI covers everything.

## Consequences

- One lockfile gives consistent dependency versions across services.
- Atomic changes across contracts, libraries and services.
- CI must stay fast as the repository grows: later we build only affected packages.
- Libraries remain independently versioned and publishable thanks to separate packages.
