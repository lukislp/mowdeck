# ADR 0063: Matter server (mowers in Apple Home and Google Home)

## Status

Accepted (2026-10-28)

## Context

The owner wants the mower as a simple device in Apple Home (iPhone) and Google Home: start, stop and the state, without the MowDeck app. Matter is the common way into both; Home Assistant also commissions Matter devices.

Forces:

- **No Matter mower device type.** Matter 1.3, 1.4, 1.4.x and 1.5 (November 2025) define none; 1.5 added cameras, closures, soil sensors and energy features ([MatterAlpha, July 2026](https://www.matteralpha.com/explainer/matter-robot-lawn-mowers-speculation): mowers "will not be supported until they are added as a device to the specification"). The closest type is the **Robotic Vacuum Cleaner** (RVC, device type `0x74`, Matter 1.2): RVC Run Mode (start/stop), RVC Operational State (state, pause, resume, go home) and optionally Power Source (battery). A plain On/Off plug would lose pause, the dock and the state.
- **Controllers.** Apple Home renders Matter robot vacuums since iOS 18.4 (start/stop tile, pause, return to dock, status, battery; scenes and automations); Google Home lists RVC (Matter 1.2) as supported. Apple Home handles **bridged** robot vacuums badly (stuck on "Updating", Siri unreliable); standalone nodes work ([Home Assistant Matter Hub](https://riddix.github.io/home-assistant-matter-hub/devices/robot-vacuum), "server mode").
- **Home Assistant route.** Home Assistant Matter Hub already maps a `lawn_mower` entity (the MowDeck integration provides one) to an RVC. It covers the owner's case without MowDeck code, but needs Home Assistant; MowDeck itself is self-hosted for people without it.
- **Network.** Matter over IP needs mDNS and IPv6 link-local on the LAN. A container on Docker's bridge network cannot do that; the server must run on the host network (Kubernetes: `hostNetwork`), and every port it opens must be firewalled to the LAN.
- **Credentials.** The server acts for the mower's account, without a person signed in. MowDeck already has API tokens with explicit grants, capped at their creator's rights, exchanged for 10 minute access tokens (Home Assistant, the iOS widgets of [ADR 0059](0059-ios-widgets-and-live-activity.md)). Personal access tokens are being reworked in parallel (ADR 0064); the server uses whatever an API token is.
- **Safety.** The emergency stop must stay where it is reliable and audited (the app, the watch, the mower's button); a Home app tile or a voice command must not become the stop path, and must not start blades unexpectedly beyond what "start" means in MowDeck.

## Decision

### A native Matter server, optional

`services/matter-server` (TypeScript, [matter.js](https://github.com/matter-js/matter.js) 0.17, Node 24 running the TypeScript directly) is an optional service: Compose profile `matter`, Helm `services.matterServer.enabled` (both off by default). The Home Assistant route stays documented as the alternative for installations that already run Home Assistant.

### One standalone node per mower

One process serves **one Matter node per mower** (no bridge aggregator): each mower is commissioned on its own, like a lamp. Every node has its own UDP port (from `MATTER_PORT_BASE`), its own random setup passcode (never one of the passcodes Matter Core § 5.1.7.1 forbids: a forbidden draw is drawn again and matter.js' `isValidPasscode` must agree) and its own 12-bit discriminator. Passcode, discriminator and port are kept per mower in the server's volume (`nodes.json`, mode 0600), the fabrics in matter.js' storage beside it, so codes and pairings survive restarts and updates.

- **New mowers** appear by themselves: the server reads the mowers of every configured account every minute.
- **Removed mowers** lose their node only after two complete answers in a row without them (a failed answer never removes anything): factory reset (fabrics, sessions and keys erased, Matter Core § 13.4), then the record is deleted.
- **Reset pairing** (web app) factory-resets one node and gives it a new passcode and discriminator on the same port: every controller loses the mower, only the new code pairs it again.

### Device type behind a profile: RVC now, a mower type when the CSA publishes it

The clusters live in one **device profile** (`src/profiles/`), behind a small interface: `createEndpoint(snapshot, labels, onIntent)` and `apply(endpoint, snapshot)`. The rest of the server only knows a normalized snapshot (activity, battery, charging) and five intents (`start`, `stop`, `pause`, `resume`, `dock`). The profile today is **`rvc`**:

| MowDeck | Matter (RVC) |
| --- | --- |
| mowing / paused | Operational State Running / Paused, Run Mode "Mähen" |
| returning | SeekingCharger |
| docked, charging (or updating) | Docked / Charging |
| idle | Stopped |
| error | Error (Stuck) |
| offline, no state yet | Error (UnableToStartOrResume) |
| battery % | Power Source `BatPercentRemaining` (half percent), charge level (warning below 20 %, critical below 10 %), charge state |

| Controller | MowDeck command |
| --- | --- |
| Run Mode → Mähen (start) | `resume_mow` when paused, else `start_mow` with every area of the map and the mower's saved mowing defaults (`mow_defaults.pattern`), like the widgets and the watch |
| Run Mode → Bereit (stop) | `return_to_dock` with `end_job` |
| Pause / Resume | `pause_mow` / `resume_mow` |
| Go Home | `return_to_dock` (the job can be resumed) |

An offline, updating or unknown mower refuses every command (the controller shows an error); invalid commands answer `CommandInvalidInState`. **There is no emergency stop over Matter**: it stays in MowDeck. **When the CSA publishes a mower device type**, a second profile implements the same interface and `MOWDECK_MATTER_DEVICE_PROFILE` selects it; nothing outside `profiles/` changes. Switching an installed node's device type needs pairing again (controllers cache the type).

The nodes announce Matter's test vendor id `0xFFF1` (MowDeck is not certified): Apple and Google show "uncertified accessory" before adding it.

### Credentials and authorization

- **Server → MowDeck:** one ordinary **API token per account**, created by an account admin with exactly `mower.view` and `mower.control` on the account (Compose: `infra/compose/secrets/matter/api-tokens`, one per line; Helm: `secrets.MATTER_API_TOKENS`). It is exchanged for 10 minute access tokens; Fleet checks every command per command kind as for any client, and the audit log shows the token. Revoking the token stops the server for that account. No new Identity concept: the server reuses whatever an API token becomes (ADR 0064).
- **Web app → server:** the admin API (`/api/v1/matter`, through Traefik or the Ingress) takes the user's access token and asks Identity (`POST /identity/me/permissions` with that token) for `can_view` and `can_control` per mower: the list and the codes only for mowers the user may control (another mower: 404 when invisible, 403 when visible). Codes are written only into responses (`Cache-Control: no-store`), never into a log line; matter.js' own "uncommissioned" log line with the QR code is suppressed.
- The server publishes a module descriptor with the ui_area `/settings/matter` (`mower.control`); Identity lists it through `IDENTITY_EXTRA_MODULE_URLS` (Compose) or by itself (Helm), and the web app shows the **Matter** settings section only then.

### Networking

Compose runs the service with `network_mode: host`; Traefik and Identity reach its admin API through `host.docker.internal` (`host-gateway`), and it reaches MowDeck's API through Traefik on the host (`MATTER_API_URL`, default `http://127.0.0.1:<GATEWAY_HTTP_PORT>`). The operator opens UDP 5353 (mDNS) and the node ports to the LAN (IPv4 and IPv6 link-local) and the admin port to Docker's bridge only ([docs/ops/matter.md](../ops/matter.md)). The controller (Apple TV or HomePod as home hub, Google Nest hub) must be on the same layer-2 network, or mDNS and IPv6 must be bridged to it.

## Consequences

- Apple Home and Google Home show each mower as a **robot vacuum**: start/stop, pause, return to dock, state and battery, usable in scenes, automations and with Siri. Mower-specific features (areas, cutting height, patterns, the camera) stay in MowDeck. A real-device test with Apple Home and Google Home is required; CI commissions every node with a matter.js controller on loopback and drives it, which proves the protocol but not each ecosystem's UI.
- A new service on the host network: its ports are part of the attack surface and must be firewalled; the admin API is authenticated per request by Identity.
- MowDeck's first TypeScript service: a Node image in the release (signed and verified like the others: `verify-release.sh` picks it up from the Compose file), its tests run in the web job (`pnpm -r test`), its lint config is its own (the workspace config leaves `services/` to the Python tooling).
- A lost `matter-data` volume means pairing every mower again in every controller.
- When Matter adds a mower type: add a profile, document the migration (reset pairing per mower), and consider making it the default.
