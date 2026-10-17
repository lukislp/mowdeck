# Licensing

| Part | License |
| --- | --- |
| Everything by default: `services/*`, `apps/*`, `tools/*`, `infra/*`, `bridge/*`, `integrations/*`, docs | [AGPL-3.0](LICENSES/AGPL-3.0.txt) (the root `LICENSE`) |
| Reusable libraries: `libs/*` | [Apache-2.0](LICENSES/Apache-2.0.txt) |
| Exception: `libs/mowdeck-control-mammotion` (imports the GPL-3.0 library PyMammotion) | [GPL-3.0](LICENSES/GPL-3.0.txt) |
| Exception: `integrations/agora-bridge` (the camera relay's optional Agora source; its own `LICENSE`) | [Apache-2.0](LICENSES/Apache-2.0.txt) |

Each library under `libs/` ships its own `LICENSE` file and declares the license in its `pyproject.toml`. Libraries are Apache-2.0 unless a GPL dependency forces GPL-3.0; that is the case only for the PyMammotion adapter package, see [ADR 0011](docs/adr/0011-gpl-package-for-pymammotion-adapter.md). `libs/mowdeck-control` itself never imports PyMammotion.

Why: the services and apps are a network service, so AGPL-3.0 ensures that modified hosted versions stay open. The libraries (control, contracts, auth and so on) are meant to be reused by other projects without copyleft obligations. See [ADR 0005](docs/adr/0005-licensing.md).

Third-party components (Traefik, NATS, PostgreSQL, OpenFGA and so on) keep their own licenses; they are used as unmodified container images.

The Agora SDK (proprietary) is never part of this repository or of a published image: `integrations/agora-bridge` downloads it while the operator builds that image, which means accepting Agora's terms. It is not part of MowDeck and has to be built by each operator; Agora's terms exclude GPLv3/LGPLv3/AGPLv3 code from the applications that use the SDK, which is why the bridge is a separate Apache-2.0 program ([ADR 0045](docs/adr/0045-camera-relay.md), [agora-bridge README](integrations/agora-bridge/README.md)).
