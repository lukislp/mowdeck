# web

The MowDeck web app: a SvelteKit 2 single-page PWA (Svelte 5, Vite 8, adapter-static with the `200.html` fallback, `ssr = false`), German by default with English. In the Compose stack the service `web` serves it at `/` on the same origin as the API.

Licence: AGPL-3.0-only (see the repository `LICENSE`). Decisions: [ADR 0020](../../docs/adr/0020-web-app-architecture.md) (architecture) and [ADR 0021](../../docs/adr/0021-browser-auth-flow.md) (authentication in the browser). Developer guide: [docs/dev/web-app.md](../../docs/dev/web-app.md). Design: [docs/dev/design-web-app.md](../../docs/dev/design-web-app.md).

## Dev loop

Requirements: Node 22 (22.12 or newer), pnpm 10.34.6 (`corepack enable`), Docker for the Compose stack. Commands run from the repository root.

```bash
docker compose -f infra/compose/docker-compose.yml up -d --build --wait   # the API (and the built app at http://localhost)
pnpm install
pnpm dev                          # Vite at http://localhost:5173
```

The dev server proxies `/api` and the Realtime WebSocket to `MOWDECK_DEV_GATEWAY` (default `http://localhost`, the stack's Traefik) and rewrites the socket's `Origin` to the gateway's, so the app and the API share one origin like in production. Register an account at `/register` and a mower with connection kind `simulated`.

| Command | What it does |
| --- | --- |
| `pnpm --filter web dev` (`pnpm dev`) | Compiles the messages (Paraglide) and starts Vite with the API proxy |
| `pnpm --filter web lint` | ESLint (including `mowdeck/no-literal-text`), Prettier check, Stylelint (`mowdeck/use-tokens`), i18n check |
| `pnpm --filter web check` | Paraglide compile, `svelte-kit sync`, `svelte-check --fail-on-warnings` |
| `pnpm --filter web test` | Vitest with coverage: 80 % lines for the session, the Realtime client (including manual driving), the stores (including the inbox), problem parsing, command availability, the push subscription flow and payload parsing, the connection form logic, the schedule form, preview and recurrence texts, the RTK range loader, the drive pilot, the health series, the plan limits and refusals, and the operator console's plan editor, assignment and service probes |
| `pnpm --filter web build` | Static build into `build/`, then `scripts/csp-hashes.mjs` writes `build/csp.conf` (fails on inline styles) |
| `pnpm --filter web i18n:check` | Both message files have the same keys, parameters and no empty text |
| `pnpm --filter e2e-web test` | Playwright against the stack (Phase 2 to Phase 4 gates), see `tests/e2e-web`; `operator.spec.ts` grants an operator admin with `docker compose exec identity python -m identity.cli grant-platform-admin` (`MOWDECK_E2E_PLATFORM_ADMIN=off` skips it) |
| `pnpm --filter e2e-web screens` | Review screenshots into `apps/web/screenshots/` |

## Plans and the operator console

The account page shows the account's plan (Entitlements' `GET /me`: status, limits with the current use, features; with billing disabled a note that the installation has no subscription limits), plan refusals (409 `plan-limit`, 403 `feature-not-in-plan`) appear in words with the limit or the feature, and the mower pages carry a banner while the plan is in grace or expired. The module `operator` (`/operator`: accounts with plan history and plan assignment, plans with an editor, system facts and service reachability) is shown only when Identity's operator endpoint answers 200; it never shows mower data. Details: [docs/dev/web-app.md](../../docs/dev/web-app.md#plans) and [Operator console](../../docs/dev/web-app.md#operator-console).

Billing is optional: the account page probes `GET /api/v1/billing/status` (only for users with `billing.manage`). A network error, a 404 or a non-JSON answer (the SPA fallback) means billing is disabled and hides every billing element; otherwise the plan section lists the plans on sale with prices, Upgrade (Stripe Checkout), Manage subscription (customer portal), the subscription status and the invoices. The legal pages (`/legal/imprint`, `/legal/terms`, `/legal/privacy`) render the operator's Markdown from `static/legal/` (placeholders with TODOs, replace them with a volume) and are linked from the sign-in page only with `MOWDECK_WEB_LEGAL_ENABLED=true`. Details: [Billing](../../docs/dev/web-app.md#billing).

AI assist ("KI-Hilfe", Vision service, `/api/v1/vision`): on the mower page a stuck mower (state `error`) offers "Recover with AI assist" and a mower standing away from the dock (idle or paused, not charging) "Find dock with AI assist", both for users who may drive it. The sheet (`AssistSheet`) names the provider that answers (the user's own model, the installation's model or none with a link to `/settings/ai-provider`), sends a JPEG snapshot of the camera video when the page has a `video[data-camera-video]` (otherwise the service asks the mower), polls the proposal once a second and shows situation, confidence, the steps in words, risks, rationale and the expiry; Approve drives the mower with the blades off, Reject (or Cancel while it drives) ends it; the outcome offers a retry when the service allows one. `?assist=<proposal id>` (the inbox notification `vision_proposal`) opens the sheet on that proposal, and an open proposal of the mower is shown instead of a new request. The settings page `/settings/ai-provider` ("KI-Anbieter", module `ai-provider`, permission `mower.drive`) configures the own model with a write-only key. Details: [AI assist](../../docs/dev/web-app.md#ai-assist).

## Generated types

`src/lib/generated/*.ts` is generated, never edited:

1. `uv run poe openapi` exports the services' OpenAPI documents to `openapi/<service>.json` (checked by `uv run poe openapi-check` and `poe test`).
2. `pnpm codegen` (`tools/ts-codegen`) writes `identity.ts`, `fleet.ts`, `telemetry.ts`, `device-gateway.ts`, `notifications.ts`, `scheduler.ts`, `entitlements.ts` (openapi-typescript) and `realtime.ts` (the WebSocket protocol, from the contracts' JSON Schemas). `pnpm --filter ts-codegen check` fails on drift.

## Docker image

`Dockerfile` (build context: the repository root): `public.ecr.aws/docker/library/node:22.23.3-alpine` installs the workspace with `--frozen-lockfile` and builds; the runtime is `public.ecr.aws/nginx/nginx-unprivileged:1.30.5-alpine` (uid 101, port 8080) with `nginx/default.conf` and `nginx/security-headers.conf` (CSP from `build/csp.conf`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, `X-Frame-Options: DENY`, COOP). `/_app/immutable/` is cached for a year, everything else revalidated, unknown paths get `200.html`; `/healthz` answers `ok`. Compose runs it read-only with a tmpfs at `/tmp`.

### Map tiles (runtime configuration)

The map draws a metric grid and contacts no third party unless the installation configures raster tiles. At container start `nginx/40-mowdeck-runtime-config.sh` (in `/docker-entrypoint.d`) writes `/tmp/mowdeck/config.json` (served at `/config.json`, `no-cache`) and `/tmp/mowdeck/csp.conf` (the build's CSP with the tile origin added to `img-src`):

| Variable (Compose `.env`) | Default | Meaning |
| --- | --- | --- |
| `MOWDECK_WEB_MAP_TILES` (`WEB_MAP_TILES`) | empty | Raster tile URL template with `{z}`, `{x}`, `{y}` and optionally `{s}`; empty keeps the grid |
| `MOWDECK_WEB_MAP_ATTRIBUTION` (`WEB_MAP_ATTRIBUTION`) | empty | Attribution text the provider requires; `openstreetmap.org` URLs get "© OpenStreetMap contributors" |
| `MOWDECK_WEB_MAP_MAX_ZOOM` (`WEB_MAP_MAX_ZOOM`) | `19` | Highest zoom the tile server delivers (1 to 22) |
| `MOWDECK_WEB_LEGAL_ENABLED` (`WEB_LEGAL_ENABLED`) | `false` | `true` shows links to `/legal/imprint`, `/legal/terms` and `/legal/privacy` on the sign-in page (`legalEnabled` in `/config.json`) |

```bash
docker run --read-only --tmpfs /tmp -p 8080:8080 \
  -e MOWDECK_WEB_MAP_TILES='https://tile.example.org/{z}/{x}/{y}.png' \
  -e MOWDECK_WEB_MAP_ATTRIBUTION='© Example' mowdeck/web:dev
```

Privacy: with tiles every map view sends the shown tile coordinates (the location of the garden) and the viewer's IP address to the tile server. Respect the provider's usage policy (not the public OpenStreetMap servers for regular use: https://operations.osmfoundation.org/policies/tiles/); a key in the template is visible to every user. Details: [docs/dev/web-app.md](../../docs/dev/web-app.md#map).

```bash
docker build -f apps/web/Dockerfile -t mowdeck/web:dev .
docker compose -f infra/compose/docker-compose.yml up -d --build web
```

## Layout

| Path | Content |
| --- | --- |
| `svelte.config.js`, `vite.config.ts` | SvelteKit (adapter-static, service worker registered by the app), Vite (Paraglide plugin, API proxy, Vitest) |
| `messages/de.json`, `messages/en.json`, `project.inlang/` | Paraglide messages (German is the base locale) and project settings |
| `src/app.html` | Document shell; loads `theme-init.js` before the first paint |
| `src/service-worker.ts` | Precaches the shell per version, network-first navigations with the cached `200.html`, never touches `/api`; Web Push: `push`, `notificationclick`, `pushsubscriptionchange` |
| `src/lib/api/` | `openapi-fetch` clients per service, the fetch wrapper (bearer, `X-Correlation-ID`, one retry after refresh on 401), `ApiProblem` |
| `src/lib/auth/session.svelte.ts` | Access token in memory, cookie-only refresh, Web Lock `mowdeck-refresh`, BroadcastChannel `mowdeck-auth` |
| `src/lib/realtime/client.svelte.ts` | One socket per tab, ref-counted subscriptions, backoff, re-auth on refresh; manual drive sessions (`startDrive`, `sendFrame` with the client-side rate limit, `stopDrive`, `onDrive`) |
| `src/lib/stores/` | `mowers` (newest `observed_at` wins, then the newest `heartbeat_at`; offline when the gateway's link heartbeat is older than 45 s or the state says `connected: false`, see `src/lib/mower/presence.ts` and ADR 0040), `commands` (pending until result, polling fallback), `inbox` (notifications, unread count, polled every 60 s and on focus), `ui` (toasts, theme, install prompt, update) |
| `src/lib/push/` | `device.ts` (push support, subscription flow, this device), `payload.ts` (the `WebPushPayload` for the service worker), `store.ts` (VAPID key and renewed subscription in the Cache API, shared with the worker) |
| `src/lib/notifications/` | Kind and channel texts and tones, the inbox and push singletons wired to the API |
| `src/lib/connection/` | Connection forms from `GET /api/v1/fleet/connection-kinds`: state, validation, options to send, 422 mapping (`form.ts`), texts (`text.ts`), the loader (`kinds.ts`) |
| `src/lib/schedules/` | Schedules: types, recurrence in words and countdowns (`recurrence.ts`), preview and verdict badges (`preview.ts`), outcome and verdict looks (`labels.ts`), form state, checks, bodies and 422 mapping (`form.ts`), loaders and rights per target (`runtime.ts`) |
| `src/lib/drive/` | Manual drive: frame cadence, speed limit, blades and rates (`pilot.ts`), session end and error texts (`labels.ts`) |
| `src/lib/health/` | Health page helpers: 30 UTC days, bar geometry, battery path, threshold level, durations (`series.ts`) |
| `src/lib/fleet/` | Fleet: site and group forms, checks and bodies, sections by site, filters and quick filters (`places.ts`), bulk targets and outcomes per mower (`bulk.ts`), the loader of sites, groups and mowers and the outcome and level words (`data.ts`) |
| `src/lib/reports/` | Reports: periods in UTC days, per mower rows, totals, faults by code, daily series and the export table (`aggregate.ts`); CSV (BOM, CRLF, `de` and `en` dialects) and the tab separated clipboard table (`csv.ts`) |
| `src/lib/live.svelte.ts` | Wires session, socket and stores; permission cache per object (60 s) |
| `src/lib/modules/<name>/module.ts`, `src/lib/shell/registry.svelte.ts` | Built-in modules and the registry fed by `GET /api/v1/identity/ui-areas` |
| `src/lib/i18n.ts` | Exhaustive enum translations, `Intl` formatting, problem messages |
| `src/lib/mower/commands.ts` | Which command is available in which activity |
| `src/lib/mower/route.ts` | Route window (1 h, today, 24 h) and live appends of the driven route |
| `src/lib/mower/rtk-range.ts`, `map-labels.ts`, `area-names.ts` | Time range and paged loading of the RTK signal map; the translated `MowerMap` labels and the area display names (custom name, own name, "Bereich N"); saving custom area names in Fleet |
| `src/lib/config.ts` | Runtime config from `/config.json` (map tiles, legal links) with a typed default |
| `src/lib/vision/` | AI assist: typed calls of the Vision API (`api.ts`), the proposal store with polling and decisions (`assist.svelte.ts`), step, situation, state and risk texts (`labels.ts`), the provider form (`provider.ts`), problem texts (`problems.ts`) and the camera snapshot (`snapshot.ts`) |
| `src/lib/billing/` | Billing probe (`GET /api/v1/billing/status`, cached per page load), price and status formatting (`billing.ts`), subscription and invoices state (`account.svelte.ts`) |
| `src/lib/legal/` | The safe Markdown subset of the legal pages (`markdown.ts`) |
| `src/lib/components/` | App components: `AreaPlan` (schematic map, fallback of `MowerMap`), `AuthFrame`, `LiveStatus`, `ConnectionForm` (generated connection options), `NotificationBadge`, `ToneBadge` (verdicts and run outcomes), `RainStrip` (forecast bars), `SchedulePreview`, `ScheduleForm`, `ScheduleEditor`, `SiteLocation`, `StopBar` (the emergency stop of mower sub-pages), `DailyBars` and `BatteryTrace` (health charts in SVG), `FleetTabs` (map, sites, groups), `BulkSheet` (confirm a bulk command, then the outcome per mower), `MowerPlacement` (site and groups of a mower), `LevelPicker` (account, site, group or mower of a role assignment), `PlanPanel`, `BillingPanel` (plans on sale, checkout, portal, invoices), `AssistSheet` (AI assist on the mower page), `AreaRenameForm` (rename a map area in MowDeck), `BridgeLinkPanel` and `BridgeLinkSheet` (the Bluetooth bridge of a mower, ADR 0038), `FirmwarePanel` (firmware check and update) |
| `src/lib/generated/`, `src/lib/paraglide/` | Generated, do not edit |
| `src/routes/` | `/login`, `/register`, `/legal/[page]`, and under `(app)`: `/mowers`, `/mowers/new`, `/mowers/[id]`, `/mowers/[id]/settings`, `/mowers/[id]/drive`, `/mowers/[id]/health`, `/mowers/[id]/map` (forwards to the mower page), `/notifications`, `/notifications/settings`, `/schedules`, `/schedules/new`, `/schedules/[id]`, `/schedules/[id]/runs`, `/fleet`, `/fleet/sites`, `/fleet/groups`, `/reports`, `/settings/users`, `/settings/api-tokens`, `/account` |
| `static/` | `manifest.webmanifest`, `theme-init.js`, `icons/` (rendered by `scripts/render-icons.py`), `legal/` (placeholder imprint, terms, privacy in Markdown) |
| `scripts/` | `csp-hashes.mjs`, `i18n-check.mjs`, `render-icons.py` |
| `openapi/` | Exported OpenAPI documents (input of the code generator) |
| `nginx/`, `Dockerfile` | The runtime image; `nginx/40-mowdeck-runtime-config.sh` renders `/config.json` and the CSP at container start |

## Pages

- `/mowers`: list with live status, quick filters (all, working, attention, at the dock) from four mowers on and collapsible sections per site when the mowers live on more than one site; `/mowers/new`: register a mower (name, connection kind and the options form of that kind, generated from Fleet's `GET /connection-kinds`).
- `/mowers/[id]`: status readouts, the map (`MowerMap`: areas, no-go zones, dock, the live mower and the driven route of the last hour, today or 24 h; full screen on phones; the schematic `AreaPlan` as the fallback), start areas chosen by tapping the map and confirmed in the picker sheet (order of taps is the mowing order), blade height and speed steppers with the capability ranges, recent commands and the command bar with the emergency stop. Areas show their MowDeck name (custom name, the mower's own name, else "Bereich N" in map order); users with `can_control` rename them from the area list, the picker or the edit button on a map label. Users without `can_control` see no command buttons, no setpoint controls and no rename buttons, only the stop.
- `/mowers/[id]` links to the health page and, with `manual_drive` and `can_drive`, to the drive page; it also shows the next schedule of the mower with its rain verdict, and the RTK signal panel (today, 7 days, 30 days; shares of fixed, float, none; the signal map on the map).
- `/mowers/[id]/drive`: manual drive with the joystick (when the mower has `manual_drive` and the user `can_drive`): session strip, speed limit 25/50/100 %, two-step blade switch, Stop and the emergency stop, a live map following the mower; the session ends when the page is left, the tab is hidden or the socket drops. Safety notes: [docs/dev/web-app.md](../../docs/dev/web-app.md#manual-drive).
- `/mowers/[id]/health`: blade hours since the change against the interval, totals, the last 30 days as bar strips, the battery of the last 24 hours, errors by code, blade changes and "blades replaced" (`can_manage_mowers`). Details: [docs/dev/web-app.md](../../docs/dev/web-app.md#health).
- `/mowers/[id]/settings`: site and groups of the mower (move to another site, join or leave groups), the site (name, time zone, coordinates from the map origin or typed; the rain pause location) and name, connection kind and its generated options form (alternatives as a segmented choice, secrets write-only and marked "set", advanced options folded, Fleet's 422 field errors on the inputs).
- `/notifications`: the inbox (kind badge, title, text, mower, time, unread marker; opening marks read; mark all read; theft alarms with acknowledge and the reported position); the unread count is a badge on the bell in the navigation. `/notifications/settings`: kinds, mowers, quiet hours, channels, Web Push on this device and the account's subscriptions, ntfy target, test notification. Flow and deployment notes (HTTPS, iOS): [docs/dev/web-app.md](../../docs/dev/web-app.md#notifications-and-web-push).
- `/schedules`: schedules with recurrence, next run and countdown, rain verdict, last outcome, enabled switch, skip next, run now (respect the rain pause); `/schedules/new` and `/schedules/[id]`: the editor with the preview (next firings, rain verdict, forecast strip); `/schedules/[id]/runs`: the runs with outcome badges and command links. Details: [docs/dev/web-app.md](../../docs/dev/web-app.md#schedules).
- `/fleet`: the fleet map (`FleetMap`): every visible mower live, filtered by site and group, a legend with counts per state, the mowers without a position, the selected mower's card, and "pause all" and "all to dock" for the filter with the outcome per mower. `/fleet/sites`: sites (default first) with time zone, address, coordinates (from a mower's map origin), mowers and groups; create, edit, delete, move mowers here, bulk commands per site. `/fleet/groups`: groups with their site or across sites, members, bulk commands, a new schedule for the group. Details: [docs/dev/web-app.md](../../docs/dev/web-app.md#fleet).
- `/reports`: mowing and blade time, distance, jobs, faults by code and availability per site (or all) and period (today, 7 days, 30 days, custom), per mower and in total, the daily mowing time; CSV download and "copy as table". Details: [docs/dev/web-app.md](../../docs/dev/web-app.md#reports).
- `/settings/users`, `/settings/api-tokens`: sub-users with their role assignments (a role at the account, a site, a group or a mower; add and remove) and API tokens (secret shown once).
- `/account`: profile, theme (System, Light, Dark), language, install, password, logout.
