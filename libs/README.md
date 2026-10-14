# libs

Reusable libraries (Apache-2.0, except `mowdeck-control-mammotion`: GPL-3.0). Each Python library is its own package in the uv workspace with a README, tests and its own LICENSE; `mowdeck-ui` is a package of the pnpm workspace (frontend), not a uv package.

| Library | Purpose |
| --- | --- |
| [`mowdeck-auth`](mowdeck-auth/README.md) | Access tokens: EdDSA JWT verification against Identity's cached JWKS, the kit principal resolver, token issuing and test helpers |
| [`mowdeck-authz`](mowdeck-authz/README.md) | Authorization: one question answered by OpenFGA (fail closed, cache), tuple writer, in-memory fake on the real model, FastAPI dependencies |
| [`mowdeck-contracts`](mowdeck-contracts/README.md) | API and event formats as Pydantic v2 models, NATS subjects, JSON Schemas |
| [`mowdeck-control`](mowdeck-control/README.md) | `MowerConnection` adapter interface, safety controller, simulated mower, demo CLI |
| [`mowdeck-control-mammotion`](mowdeck-control-mammotion/README.md) | **GPL-3.0**: Mammotion adapter on PyMammotion (`cloud`, `ble_bridge`), owner test script |
| [`mowdeck-patterns`](mowdeck-patterns/README.md) | Mowing patterns as plug-ins, the route validation chain, chains, coverage and gaps, preview; pure computation on Shapely |
| [`mowdeck-service-kit`](mowdeck-service-kit/README.md) | Skeleton of every service: settings, logging, errors, health, event bus, tenancy, test utilities |
| [`mowdeck-ui`](mowdeck-ui/README.md) | pnpm package `@mowdeck/ui`: design system of the web app (design tokens, Svelte 5 components, the frontend lint rules, the contrast check) |

Overview: [docs/dev/libraries.md](../docs/dev/libraries.md).
