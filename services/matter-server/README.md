# matter-server

Every mower as its own Matter device for Apple Home, Google Home and Home Assistant: start, pause, return to the dock, state and battery ([ADR 0063](../../docs/adr/0063-matter-server.md)). Optional: Compose profile `matter`, Helm `services.matterServer.enabled`. Operations: [docs/ops/matter.md](../../docs/ops/matter.md); user guide: [docs/guides/matter.md](../../docs/guides/matter.md).

Licence: AGPL-3.0-only (see the repository `LICENSE`). The only TypeScript service: [matter.js](https://github.com/matter-js/matter.js) (Apache-2.0) on Node 24, which runs the TypeScript sources directly (type stripping, no build step). Not a uv workspace member and not generated from `templates/service`; it is a pnpm workspace package instead.

## Purpose

- **One standalone Matter node per mower** (no bridge): each with its own UDP port from `MOWDECK_MATTER_PORT_BASE`, its own random setup passcode (never a passcode the specification forbids) and discriminator, kept in the volume (`/data/nodes.json`, 0600) with the fabrics (`/data/matter`).
- **Device profile `rvc`** (`src/profiles/`): Robotic Vacuum Cleaner with RVC Run Mode, RVC Operational State and Power Source, because Matter has no mower type up to 1.5. A future mower device type is another profile behind the same interface.
- **Commands** from a controller go through `planIntent` (`src/model.ts`) to Fleet with the server's API token: start (resume when paused, else every area with the saved mowing defaults), stop (dock and end the job), pause, resume, go home. No emergency stop. Offline or updating mowers refuse everything.
- **Sync** every 60 s: new mowers get a node; a mower missing from two complete answers in a row is factory-reset and forgotten. **Poll** every 15 s: Telemetry's compact state.
- **Admin API** for the web app (below), checked per mower with the caller's own token by Identity.

## Interfaces

| Method | Path | Who | Answer |
| --- | --- | --- | --- |
| GET | `/api/v1/matter/mowers` | token | Mowers with a node the caller may control: `mower_id`, `name`, `commissioned`, `fabric_count` |
| GET | `/api/v1/matter/mowers/{id}/pairing` | `can_control` | `manual_code` (`1234-567-8901`), `qr_payload` (`MT:...`), `qr_matrix` (rows of `0`/`1`); `Cache-Control: no-store` |
| POST | `/api/v1/matter/mowers/{id}/pairing/reset` | `can_control` | Factory reset of the node, new passcode and discriminator; the new codes |
| GET | `/healthz`, `/readyz`, `/metrics`, `/.well-known/mowdeck-module` | anyone | Operations; the descriptor announces the ui_area `/settings/matter` (`mower.control`) |

A mower the caller cannot see answers 404, one the caller sees but may not control 403; a missing or rejected token 401. Every response carries `X-Source-Code` (AGPL-3.0 section 13).

It calls MowDeck's API (`MOWDECK_MATTER_API_URL`): `POST /identity/token` (API token exchange), `POST /identity/me/permissions` with the caller's token, `GET /fleet/mowers`, `GET /fleet/mowers/{id}` (mowing defaults), `GET /telemetry/mowers/{id}/widget` and `/map`, `POST /fleet/mowers/{id}/commands`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOWDECK_MATTER_API_URL` | `http://127.0.0.1` | MowDeck's API as the server reaches it |
| `MOWDECK_MATTER_API_TOKENS_FILE` | none | File with API tokens, one per line and account (`mower.view`, `mower.control` only) |
| `MOWDECK_MATTER_DATA_DIR` | `/data` | Fabrics and setup credentials (a volume) |
| `MOWDECK_MATTER_HTTP_HOST`, `MOWDECK_MATTER_HTTP_PORT` | `0.0.0.0`, `8790` | Admin API |
| `MOWDECK_MATTER_PORT_BASE`, `MOWDECK_MATTER_MAX_NODES` | `5540`, `16` | UDP ports of the nodes |
| `MOWDECK_MATTER_POLL_SECONDS`, `MOWDECK_MATTER_SYNC_SECONDS` | `15`, `60` | State poll and mower list |
| `MOWDECK_MATTER_LANGUAGE` | `de` | Labels the controllers show (`de`, `en`) |
| `MOWDECK_MATTER_DEVICE_PROFILE` | `rvc` | Device profile |
| `MOWDECK_MATTER_MDNS_INTERFACE` | all | Network interface for mDNS |
| `MOWDECK_LOG_LEVEL` | `INFO` | JSON log lines; matter.js itself logs warnings only |

## Development

```sh
pnpm --filter matter-server test    # unit tests and a loopback integration test with a matter.js controller
pnpm --filter matter-server check   # tsc
pnpm --filter matter-server lint
```

`MATTER_SKIP_INTEGRATION=1` skips the integration test (it opens UDP ports and mDNS sockets). Run locally against a MowDeck: `MOWDECK_MATTER_API_URL=http://localhost MOWDECK_MATTER_API_TOKENS_FILE=./tokens MOWDECK_MATTER_DATA_DIR=./.data node src/main.ts`.
