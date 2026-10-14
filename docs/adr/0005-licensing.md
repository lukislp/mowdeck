# ADR 0005: AGPL-3.0 for services and apps, Apache-2.0 for libraries

## Status

Accepted (2026-10-08). Amended by ADR 0011 (GPL-3.0 for the PyMammotion adapter package).

## Context

MowDeck is meant to be open and self-hostable. The services form a network service, and we want modifications that are offered as a hosted service to stay open. At the same time, parts such as the mower control library should be reusable by anyone, including closed-source projects.

## Decision

License services, apps, tooling and infrastructure under AGPL-3.0. License the reusable libraries under `libs/` under Apache-2.0; each library ships its own `LICENSE`. The full texts live in `LICENSES/`, the root `LICENSE` is the AGPL-3.0, and `LICENSING.md` explains the split.

## Consequences

- Hosted forks must publish their changes.
- Libraries can be adopted widely without copyleft friction.
- Contributions to a library must not copy code from AGPL parts of the repository.
- Third-party components keep their own licenses.
- Review again before any commercial distribution.
