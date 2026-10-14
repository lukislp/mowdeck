# MowDeck

MowDeck is a self-hosted control centre for robotic mowers. It runs on a mini PC or Raspberry Pi at home with Docker Compose, keeps your data in your own house, and gives you and your family one web app for everything the mower does: live status and map, starting a job with your own settings, schedules that respect the weather, the cameras, notifications on the phone, and an optional AI assistant for a stuck mower. The same images also run on Kubernetes if you prefer a server.

MowDeck is not tied to one manufacturer. The services never talk to a mower directly; they use the `mowdeck-control` interface, and each manufacturer is an adapter package behind it. The first adapter is for Mammotion (LUBA, YUKA); a simulator implements the same interface for trying everything without hardware. A Home Assistant integration and native app shells for iOS and Android come with it.

## Supported mowers

| Mowers | Adapter | Link to the mower | State |
| --- | --- | --- | --- |
| Mammotion LUBA 2 / LUBA mini / YUKA | `libs/mowdeck-control-mammotion` (GPL-3.0, built on [PyMammotion](https://github.com/mikey0000/PyMammotion)) | Manufacturer cloud today; a small Bluetooth bridge board on the mower is prepared | In daily use on a LUBA 2 AWD X |
| Simulator | `libs/mowdeck-control` | none | For development, tests and trying the app |

What a mower can do (patterns, cameras, lights, RTK, manual drive) is reported by its adapter as capabilities; the web app shows only what the connected mower supports. Adding a manufacturer means one adapter package that implements the interface (connection, state, commands, map, capabilities) and, if the manufacturer has a cloud with accounts, its account linking and device list. See [libs/mowdeck-control/README.md](libs/mowdeck-control/README.md) and [ADR 0012](docs/adr/0012-mower-control-interface-and-safety-rules.md).

## Features

**Mower and map**

- Live status with battery, blades, speed, signal and RTK fix quality, refreshed faster while the mower works.
- Map with your zones, the charging station, the mower's heading and track, the mower's own computed mow path, and heatmap layers for coverage, RTK, WLAN and mobile signal with a list of missed spots.
- Commands confirmed by the mower's real state: start, pause, resume, emergency stop, "to the station" with the choice to end the job or keep it paused.
- Start sheet: mowing pattern, angle, border laps, spacing, blade height and speed per run, remembered per mower; a path preview before you start; a rain warning with the expected duration.
- Area names you choose, model limits read from the mower (cutting height, speed), real model names in the mower list.

**Schedules and weather**

- Weekly schedules per mower and zone with a rain pause that looks at the forecast and at the past hours, so a wet lawn after a shower counts too.
- Rain readout in the mower sidebar: what the rule decided and until when.

**Cameras** (where the mower has them)

- Both front cameras live in the browser, side by side or stitched into one panorama with a calibration you choose: sliders, local feature matching in the browser, or AI-found corresponding points.
- Camera headlamp switch; stereo frames and automatic analysis for the AI assistant.

**AI assistant (optional, your own model key)**

- Any vision model through LiteLLM (OpenAI, Anthropic, Google, local Ollama and more), configured per user, keys encrypted at rest, daily caps.
- "AI recover": the assistant looks at the camera pictures and proposes a way out of an error; nothing drives until you approve it. Automatic analysis on recoverable errors with a push notification.

**Accounts and notifications**

- Manufacturer cloud accounts linked per MowDeck account (several per account), mowers added from the account's device list; one mower belongs to exactly one MowDeck account.
- Sub-users with roles, support access with a time limit, audit log, fleet views with sites and groups for more than one mower.
- Notifications as Web Push, ntfy, or directly through APNs to the iOS app; an inbox in the web app.

**Integrations and apps**

- Home Assistant integration (its own repository, see below), REST APIs with OpenAPI documents for every service, WebSocket live stream.
- iOS and Android app shells (Capacitor) that load your own MowDeck server, with local notifications.

**Operations**

- Docker Compose stack with Traefik, NATS, PostgreSQL and OpenFGA; Helm chart for Kubernetes; images for amd64 and arm64.
- Health history, battery statistics and a theft alarm.

Roadmap and what is done: [docs/phases.md](docs/phases.md).

## Repository layout

| Path | Content |
| --- | --- |
| `services/` | Deployable services, one uv workspace package each (AGPL-3.0) |
| `libs/` | Reusable libraries, e.g. `mowdeck-control` (the mower interface and the simulator), `mowdeck-contracts` (Apache-2.0); manufacturer adapters such as `mowdeck-control-mammotion` (GPL-3.0) |
| `libs/mowdeck-ui/` | Design system of the web app: tokens and Svelte 5 components (pnpm package `@mowdeck/ui`, Apache-2.0) |
| `apps/` | User-facing apps (pnpm workspace) |
| `apps/web/` | The web app: SvelteKit 2 single-page PWA, served by the Compose service `web` at `/` (AGPL-3.0) |
| `apps/mobile/` | iOS and Android app shells (Capacitor) that load the web app from the MowDeck server; Mac install scripts and the Android APK build ([guide](docs/ops/mobile-apps.md), AGPL-3.0) |
| `bridge/` | ESP32 bridge firmware ([build guide](docs/ops/ble-bridge-esphome.md)) |
| `integrations/` | Placeholder for client integrations; the Home Assistant integration `mowdeck` lives in its own repository [`lukislp/mowdeck-hacs`](https://github.com/lukislp/mowdeck-hacs) ([guide](docs/dev/home-assistant.md)) |
| `infra/compose/` | Docker Compose stack (Traefik, NATS, PostgreSQL, OpenFGA) |
| `infra/helm/` | Helm chart `mowdeck` for Kubernetes (cloud and homelab), its checks and the kind install test |
| `templates/` | Service template (later) |
| `tools/` | Repository tooling, e.g. `repo-checks` (including the OpenAPI export for the web app) |
| `tools/ts-codegen/` | Generates the web app's TypeScript API types from the OpenAPI documents and contracts JSON Schemas |
| `tests/e2e/`, `tests/e2e-web/` | Acceptance tests (pytest and Playwright) against the Compose stack |
| `docs/` | MkDocs site: arc42 architecture, ADRs, guides, original plan |

## Quick start

Requirements: Docker; for development also Python 3.13, [uv](https://docs.astral.sh/uv/), Node 22 and pnpm 10.34.6 (`corepack enable`).

```bash
cp infra/compose/.env.example infra/compose/.env   # then set the passwords and keys
docker compose -f infra/compose/docker-compose.yml up -d --build --wait
```

Then open the web app at `http://localhost`, register the first account (it becomes the owner) and add a simulated mower, or link your manufacturer account and pick your mower from its device list.

On Kubernetes (released images and chart; see [docs/ops/helm.md](docs/ops/helm.md) for the CloudNativePG operator and the secrets):

```bash
helm install mowdeck oci://ghcr.io/lukislp/charts/mowdeck --version <version> -n mowdeck --create-namespace -f my-values.yaml
```

Development:

```bash
uv sync                  # install the workspace
uv run poe lint          # ruff + mypy
uv run poe test          # pytest with coverage
uv run poe docs          # build the documentation (strict)
pnpm install && pnpm dev # web app dev server at http://localhost:5173, proxies /api to the stack
```

More: [getting started](docs/dev/getting-started.md), [compose stack](infra/compose/README.md), [web app guide](docs/dev/web-app.md), [contributing](CONTRIBUTING.md).

## Licensing

AGPL-3.0 for services, apps and tooling (see `LICENSE`); Apache-2.0 for the reusable libraries under `libs/`; GPL-3.0 for the Mammotion adapter because of its PyMammotion dependency. Details in [LICENSING.md](LICENSING.md).

## Documentation

Source in [`docs/`](docs/index.md), built with MkDocs Material (`uv run mkdocs serve`). The original project plan (German) is kept at [docs/plan/project-plan.de.md](docs/plan/project-plan.de.md).
