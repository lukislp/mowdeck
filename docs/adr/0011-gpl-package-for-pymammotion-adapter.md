# ADR 0011: Separate GPL-3.0 package for the PyMammotion adapter

## Status

Accepted (2026-10-08). Amends ADR 0005.

## Context

ADR 0005 licenses every library under `libs/` under Apache-2.0 so that the mower control library can be reused freely. The spike established that PyMammotion is licensed under GPL-3.0. A library that imports PyMammotion and is distributed is, in practice, bound by the GPL; publishing it as Apache-2.0 would be misleading and legally fragile. The `MowerConnection` interface, the simulated mower, the domain model and the command translation do not need PyMammotion at all.

Options considered:

1. Split: keep `mowdeck-control` (interface, simulator, domain) Apache-2.0 and put the PyMammotion adapter into its own package under GPL-3.0.
2. License all of `mowdeck-control` under GPL-3.0.
3. Defer the decision while the repository is private.

## Decision

Option 1. The repository gets a new library `libs/mowdeck-control-mammotion` licensed under GPL-3.0 (`LICENSES/GPL-3.0.txt`, own `LICENSE` file, `license = "GPL-3.0-only"` in `pyproject.toml`). It contains everything that imports `pymammotion`: the BLE bridge transport, the cloud transport and the translation between PyMammotion messages and `mowdeck-contracts` models. `libs/mowdeck-control` stays Apache-2.0 and must never import `pymammotion`; it depends only on `mowdeck-contracts`. The Device-Gateway service (AGPL-3.0) depends on both, which is compatible.

A repository check enforces the boundary: no module under `libs/mowdeck-control` imports `pymammotion`, and every `libs/*` package declares its license in `pyproject.toml` and ships the matching `LICENSE` file.

## Consequences

- The reusable control interface and the simulator remain usable in closed-source projects.
- Anyone who uses the Mammotion adapter outside MowDeck inherits the GPL-3.0 obligations of PyMammotion, which is unavoidable.
- `LICENSING.md` and the `libs/` table are updated; the rule "libs are Apache-2.0" becomes "libs are Apache-2.0 unless a GPL dependency forces GPL-3.0, which is then stated per package".
- Other adapters with permissive dependencies (for example a future vendor SDK under MIT) can stay Apache-2.0.
