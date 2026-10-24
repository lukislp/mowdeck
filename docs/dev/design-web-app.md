# Design: MowDeck web app (Phase 2 MVP)

Status: approved 2026-10-09. **Task 10 is implemented** (`apps/web`, `libs/mowdeck-ui`, `tests/e2e-web`, `tools/ts-codegen`, the backend gaps G1, G1b, G2, G3 (backend part), G4, G8 and G9; [ADR 0020](../adr/0020-web-app-architecture.md) and [ADR 0021](../adr/0021-browser-auth-flow.md) record the decisions, the developer guide is [Web app](web-app.md)). **Task 11 (map) is implemented** too ([ADR 0023](../adr/0023-map-rendering-and-projection.md)), and so are **Tasks 12 and 13** (notifications in the PWA, connection forms) and, in Phase 3, **Task 17** (schedules with rain pause and the RTK signal map), in Phase 4 **Tasks 21 and 22** (fleet management and reports, the operator console and the plan display). The corrections against this draft are listed in section 14 and win over the rest of the text. Scope: Tasks 10 to 13 of Phase 2. Inputs: `docs/plan/project-plan.de.md` (Funktionsumfang MVP, Oberfläche, Erweiterbarkeit, Gemeinsame Bibliotheken, Tech-Stack, Risiken), ADR 0003 (SvelteKit), ADR 0012 (safety rules), ADR 0015 (tokens and sessions), ADR 0016 (commands), ADR 0018 (Realtime protocol), the service READMEs and the route modules on `main`.

Conventions in this document: "verified" means checked against the repository or a fetched source in this session; "unverified" marks claims taken from memory or from search summaries that were not opened. Package versions were read from the npm registry (`https://registry.npmjs.org/<package>`) on 2026-10-09.

## 0. Summary of decisions

| Topic | Decision |
| --- | --- |
| Framework | SvelteKit 3.0.x (Svelte 5.57, runes), `@sveltejs/adapter-static` 4.0.x, SPA mode with `200.html` fallback, Vite 8, TypeScript 6.0.x |
| PWA | SvelteKit's native service worker (`src/service-worker.ts`) plus a hand-written manifest; not `@vite-pwa/sveltekit` (its peer range ends at Kit 2) |
| Serving | New `web` service: nginx (unprivileged, read-only) behind Traefik at `/`, same origin as `/api/v1/*`, so no CORS |
| Shared UI | `libs/mowdeck-ui`: Svelte 5 component library in a pnpm workspace; design tokens as CSS custom properties |
| Types | Generated from committed OpenAPI documents (exported in Python, no running stack) and the contracts JSON Schemas; CI freshness check |
| Auth | Access token in memory, refresh token only in the existing `mowdeck_refresh` HttpOnly cookie; cross-tab single-flight refresh with the Web Locks API |
| Modules | Built-in route modules in the app; runtime enablement and labels from one new aggregation endpoint in Identity |
| Realtime | One socket per tab, ref-counted subscriptions, backoff with jitter, re-auth on refresh, per-mower state store |
| i18n | Paraglide JS 2 (compiled, typed messages), `messages/de.json` default, `messages/en.json` |
| Map | Leaflet 1.9.4; geometry stays in local metres on the wire; the browser converts with a TypeScript port of `mowdeck_control.geo` checked against golden vectors; Geoman deferred to the first editing feature |
| Map backend | Device-Gateway publishes map and capabilities snapshots to a new last-value stream; Telemetry stores and serves them |
| Visual direction | "Instrument panel" (owner decision): sober, dense, monospace readouts, colour means state, map at the centre |
| Gate | Playwright suite `tests/e2e-web` against the Compose stack, CI job `e2e-web` |

## 1. Architecture

### 1.1 Repository layout

```
pnpm-workspace.yaml            # apps/*, libs/mowdeck-ui, tests/e2e-web, tools/ts-codegen
package.json                   # root scripts only (private), packageManager pnpm@12.10.1
.nvmrc / .node-version         # 22.23.3
apps/web/                      # SvelteKit app (the PWA)
  vite.config.ts               # SvelteKit 3 config lives here (svelte.config.js is gone)
  messages/{de,en}.json        # Paraglide messages
  project.inlang/settings.json
  src/
    app.html
    service-worker.ts
    lib/
      api/                     # typed clients (openapi-fetch), problem+json handling
      auth/                    # session.svelte.ts, refresh lock
      realtime/                # connection manager, protocol types
      stores/                  # mowers.svelte.ts, commands.svelte.ts, theme, install
      modules/<name>/module.ts # module definitions (route, label key, permission, icon)
      shell/                   # registry, navigation, layout
      generated/               # committed generated types (do not edit)
    routes/                    # SvelteKit routes (thin; pages import module components)
  static/manifest.webmanifest, icons/, theme-init.js
  Dockerfile, nginx/
libs/mowdeck-ui/               # Svelte component library (no app state, no API calls)
  src/lib/styles/tokens.css    # design tokens (section 12)
  src/lib/{map,mower,status,command,permission,form,layout}/
tests/e2e-web/                 # Playwright gate (own package)
tools/ts-codegen/              # type generation, isolated TypeScript 5.9 (see 1.4)
```

`pnpm-workspace.yaml` lists `apps/*`, `libs/mowdeck-ui`, `tests/e2e-web` and `tools/ts-codegen`. The brief asked for `apps/*` and `libs/mowdeck-ui`; the two extra entries keep Playwright and the code generator out of the app's dependency tree (Playwright browsers are large; `openapi-typescript` 7.13.0 declares `typescript: ^5.x` as peer while the app needs TypeScript 6, verified on the registry). The Python uv workspace is untouched; `libs/*` Python packages are not pnpm packages.

### 1.2 SvelteKit and adapter choice

- **SvelteKit 3.0.1** was published on 2026-10-06 (registry), three days ago. Its minimums are Node 22.17, TypeScript 6, Svelte 5.56.4, Vite 8.0.12 and vite-plugin-svelte 7 (migration guide: https://next.svelte.dev/docs/kit/migrating-to-sveltekit-3, release candidate post: https://svelte.dev/blog/sveltekit-3-release-candidate). The dev machine runs Node 22.20.0 (verified), the Docker build stage uses `public.ecr.aws/docker/library/node:22.23.3-alpine`. Starting on 3.x avoids a migration a few weeks after the first commit (`$lib` becomes `#lib` with explicit extensions, config moves into `vite.config.ts`, `$app/stores` is gone). Fallback if a blocking 3.0 bug appears during the first days of Task 10: pin `@sveltejs/kit` 2.70.3 with adapter-static 3.0.10 and `@vite-pwa/sveltekit` 1.1.0; the module layout below works with both.
- **adapter-static 4.0.0** with `fallback: '200.html'` and `ssr = false` in the root layout (SPA mode, https://svelte.dev/docs/kit/single-page-apps; the docs recommend not naming the fallback `index.html`). The app has no server logic; everything dynamic comes from the API. A Node adapter would add a runtime with no benefit and a second process to secure. The login page and the shell are client-rendered; nothing is prerendered except a static `/offline` page for the service worker.
- **Served by nginx at `/`** behind Traefik, on the same origin as `/api/v1/*`. Consequences: no CORS configuration, the refresh cookie with `SameSite=Strict` and `Path=/api/v1/identity/session` works as is, Realtime's `Origin` check can be set to the public origin.

### 1.3 `libs/mowdeck-ui`

A Svelte 5 library built with `@sveltejs/package` (exports `.svelte` sources plus types; consumers compile them). Rules: no fetch, no stores with global state, no i18n runtime inside the library (texts arrive as props or snippets, so the library stays usable by an operator console or a Capacitor shell). Licence Apache-2.0 like the other libraries.

| Component | Purpose |
| --- | --- |
| `MowerMap` | Leaflet map: areas, no-go zones, obstacles, paths, dock, mower marker with heading, driven route, area selection mode |
| `MowerCard` | List row/card: name, activity, battery, RTK, connection, staleness |
| `StatusBadge`, `BatteryGauge`, `RtkIndicator`, `ConnectionIndicator`, `Readout` | Status primitives (monospace values, units) |
| `CommandBar` | Start, pause, resume, dock, plus the `EmergencyStop` slot that is always rendered |
| `EmergencyStop` | The stop control (section 12.7); cannot be hidden by props |
| `Can` | Permission gate: renders children when `allowed`, otherwise `fallback` or nothing |
| `Stepper`, `SegmentedControl`, `Field`, `SchemaForm` | Inputs; `SchemaForm` renders a small JSON Schema subset (connection options) |
| `AppShell`, `NavRail`, `TopBar`, `Sheet`, `Toast` | Layout |

`Can` is deliberately dumb: the app computes `allowed` (section 3.3) and the library only renders. A permission gate never replaces the server check; it only avoids offering what will be refused.

### 1.4 Generated TypeScript types

Two sources, both committed, both checked in CI like the Python schemas (`libs/mowdeck-contracts` already fails its tests when `schemas/v1` is stale):

1. **OpenAPI per service.** The services serve `/api/v1/openapi.json`, but Traefik routes only `/api/v1/<service>/*`, so that path is not reachable through the gateway (verified in `docker-compose.yml`). The generator therefore does not need a running stack: a Python script `tools/export-openapi` (poe task `openapi`) imports each service's `build_app(...)` with throwaway settings and writes `app.openapi()` to `apps/web/openapi/<service>.json` (sorted keys). The exact constructor arguments differ per service (unverified detail; Task 10 adds a `build_openapi_app()` helper per service if `build_app` needs live dependencies).
2. **Contracts JSON Schemas** (`libs/mowdeck-contracts/schemas/v1/*.json`) for payloads that are not in any OpenAPI document: `RealtimeClientMessage`, `RealtimeServerMessage`, `MowerState`, `MowerMap`, `MowerCapabilities`, `CommandResult`.

`tools/ts-codegen` (own TypeScript 5.9.3) runs `openapi-typescript` 7.13.0 (https://openapi-ts.dev/) for (1) and `json-schema-to-typescript` 16.0.0 (https://github.com/bcherny/json-schema-to-typescript) for (2), writing `apps/web/src/lib/generated/{identity,fleet,telemetry,device-gateway}.ts` and `contracts.ts`. The app calls the API with `openapi-fetch` 0.17.0 (https://openapi-ts.dev/openapi-fetch/), one typed client per service sharing one middleware (auth header, correlation id, problem+json parsing, 401 retry).

```jsonc
// tools/ts-codegen/package.json (scripts)
{ "scripts": {
  "gen": "node gen.mjs",                  // writes apps/web/src/lib/generated/*
  "check": "node gen.mjs --check"         // regenerates into a temp dir and diffs, exit 1 on drift
} }
```

CI: the `web` job runs `uv run poe openapi --check` (Python side) and `pnpm --filter ts-codegen check`. A service PR that changes a route without regenerating fails, exactly like a contracts change without new schemas.

## 2. Authentication in the browser

### 2.1 What Identity already provides (verified in `auth_routes.py`)

- `POST /api/v1/identity/session` returns `TokenOut` (`access_token`, `expires_in`, `expires_at`, `account_id`, `session_id`, `refresh_token`, `refresh_expires_at`) and sets the cookie `mowdeck_refresh`: `HttpOnly`, `SameSite=Strict`, `Path=/api/v1/identity/session`, `Max-Age` = refresh lifetime, `Secure` from `MOWDECK_IDENTITY_REFRESH_COOKIE_SECURE` (Compose development sets `IDENTITY_REFRESH_COOKIE_SECURE=false` in `.env.example`).
- `POST /api/v1/identity/session/refresh` accepts the cookie when the header `X-Requested-With` is present (any value), rotates and sets the new cookie. `POST /session/logout` works the same way and deletes the cookie.
- `GET /api/v1/identity/me` returns `MeOut`: `subject`, `kind`, `account_id`, `display_name`, `username`, `email`, `must_change_password`, `roles`, `permissions`, `plan`, `session_id`, `memberships[]`. The field is `permissions`, not `perms` (the token claim is `perms`). The app uses `me.permissions`.
- `POST /api/v1/identity/me/permissions` answers up to 100 `{relation, object}` checks (relations include `can_control`, `can_drive`, `can_emergency_stop`, `can_manage_mowers`; objects `account|site|group|mower:<uuid>`).
- Rotation has reuse detection without a grace period: presenting a rotated refresh token revokes the whole login session (`sessions.py`, verified).

So the cookie flow exists. Two gaps remain (section 7, G1 and G2): the login response also puts the refresh token in the JSON body, readable by any script on the page, and Traefik's rate limit covers the refresh route.

### 2.2 Flow

```
Login form --POST /session {username,password} + header X-Mowdeck-Refresh: cookie-only (G1)-->
   <- access_token (memory), cookie mowdeck_refresh (HttpOnly)
GET /me -> principal, permissions, memberships -> shell renders
Timer at expires_at - 60 s (with server clock skew) -> refresh (single-flight, cross-tab)
Reload / new tab -> no token in memory -> POST /session/refresh (cookie) -> token or login page
Logout -> POST /session/logout (cookie + X-Requested-With) -> clear memory, close socket, broadcast
```

- **Storage.** The access token lives only in a module-level `$state` inside `session.svelte.ts`, never in `localStorage`, `sessionStorage` or IndexedDB. A page reload costs one refresh request. The service worker never sees tokens (it does not proxy API calls).
- **CSRF.** The refresh and logout endpoints act on a cookie, so they are CSRF targets in principle. Defences already in place: `SameSite=Strict` (the cookie is not sent on cross-site requests), the narrow `Path`, and the required `X-Requested-With` header (a cross-origin request with a custom header needs a CORS preflight that the API never approves; OWASP lists custom request headers as a defence: https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html). All other API calls use the `Authorization: Bearer` header, which a cross-site page cannot set. No further CSRF token is needed.
- **Single-flight refresh across tabs.** Because a reused refresh token kills the session, two tabs refreshing at the same moment would log the user out. The refresh runs inside `navigator.locks.request('mowdeck-refresh', ...)` (Web Locks API, https://developer.mozilla.org/en-US/docs/Web/API/Web_Locks_API). Inside the lock the tab first reads a `BroadcastChannel('mowdeck-auth')` cache: if another tab obtained a token less than 30 s ago it adopts it instead of refreshing. After a refresh the tab broadcasts `{access_token, expires_at}`. Logout broadcasts `logout`. Without Web Locks (very old browsers) the app falls back to a `localStorage` timestamp mutex (best effort).
- **Lost responses.** If the network drops after Identity rotated but before the browser stored the new cookie, the next refresh presents the old token and the session ends. G2 proposes a short reuse grace in Identity; until then the app treats a failed refresh as "log in again", which is safe.
- **401 handling.** The fetch middleware retries a request once after a refresh when a 401 arrives with `WWW-Authenticate: Bearer` (the kit adds it). A second 401, or a failed refresh, clears the session and routes to `/login?next=<path>`. 403 and 404 are rendered as problem messages, never retried.
- **Problem+json.** Every non-2xx response with `application/problem+json` becomes an `ApiProblem {type, title, status, detail, correlation_id, errors[]}`. The UI shows a translated message keyed by `type` (for example `urn:mowdeck:problem:not-found`) and the `correlation_id` in a copyable detail line. Field errors map to form fields via `pointer`.
- **must_change_password.** If `me.must_change_password` is true (temporary sub-user password), the shell routes to `/account/password` before anything else.
- **Account switching.** `me.memberships` can list several accounts; login takes an optional `account_id`. Phase 2 shows a switcher only when there is more than one membership (it logs in again with that account id).
- **API token login** is not offered in the app.

## 3. Module shell

### 3.1 Built-in modules

```ts
// apps/web/src/lib/modules/types.ts
import type { Component } from 'svelte';
import type { MessageKey } from '#lib/i18n.js';

export interface AppModule {
  id: string;                    // 'mowers'
  route: string;                 // '/mowers' (must match a SvelteKit route)
  labelKey: MessageKey;          // 'nav_mowers'
  icon: Component;               // one icon from mowdeck-ui/icons
  requiredPermission: string;    // permission key, e.g. 'mower.view'
  order: number;
  core?: boolean;                // shown without a ui_area (e.g. account page)
  placement: 'primary' | 'settings';
}

// apps/web/src/lib/modules/mowers/module.ts
import IconMower from '@mowdeck/ui/icons/Mower.svelte';
export default {
  id: 'mowers', route: '/mowers', labelKey: 'nav_mowers', icon: IconMower,
  requiredPermission: 'mower.view', order: 10, placement: 'primary',
} satisfies AppModule;
```

```ts
// apps/web/src/lib/shell/registry.ts
const found = import.meta.glob<{ default: AppModule }>('../modules/*/module.ts', { eager: true });
export const builtIn: AppModule[] = Object.values(found).map((m) => m.default)
  .sort((a, b) => a.order - b.order);
```

Phase 2 modules: `mowers` (list and detail), `account` (core: profile, password, theme, language), `users` (`/settings/users`, `users.manage`), `api-tokens` (`/settings/api-tokens`, `users.manage`), `notifications` (Task 12, core for every user with `mower.view`), `audit` (`/settings/audit`, `audit.view`, read-only list, optional in Task 10).

### 3.2 `ui_areas` at runtime: decision

The descriptors are served at `/.well-known/mowdeck-module` outside `/api/v1` (kit `health.py`, verified), so the browser cannot reach them through Traefik, and listing service URLs in the app config would leak topology and duplicate Identity's `MOWDECK_IDENTITY_MODULE_URLS`. Identity already fetches every descriptor at startup and every 10 minutes for the permission catalogue. **Decision:** add `GET /api/v1/identity/ui-areas` to Identity (gap G4). It returns the merged `ui_areas` of all modules that answered, each with the module name and version, filtered to the areas whose `required_permission` is in the caller's `perms`, with `Cache-Control: private, max-age=300`.

Merge rule in the app:

- A built-in module is shown when the user holds its `requiredPermission` (from `me.permissions`) **and** either it is `core` or a `ui_area` with the same route exists (the backing service is deployed and announces it). So the menu follows the installation: no Fleet, no "Mowers".
- A `ui_area` without a built-in module is not rendered in Phase 2 (there is no code to show) and is logged once in the console. Remote UI modules (micro-frontends) are out of scope; when they come, `UiAreaDef` gets an optional `entry` URL.
- Labels: `UiAreaDef.menu_label` is a single English string today (`"Mowers"`, `"Users and roles"`). Built-in modules use their own message keys; G4 adds an optional `labels: {de, en, ...}` map to `UiAreaDef` (backwards compatible) for future areas without built-in code.
- If the endpoint fails, the app falls back to built-in modules filtered by permission only. The menu must never block the emergency stop.

### 3.3 Permission checks in the UI

Two levels:

- **Navigation and account-wide actions:** `me.permissions` (an upper bound: "held somewhere in the account"). Good enough to show a menu entry.
- **Per mower buttons:** `can_control`, `can_drive`, `can_manage_mowers`, `can_emergency_stop` on `mower:<id>` via one `POST /me/permissions` batch per detail page (and one batch for the visible list, at most 100 checks, chunked). Results are cached for 60 s and dropped on `revoked`, on token refresh and on `identity.grants.changed` effects (the socket's `revoked`). A Viewer therefore sees the stop control but no start button; an Operator on group North sees start only on North's mowers.

## 4. Pages for Phase 2

All routes are under the authenticated layout except `/login` and `/register` (the latter only shown when registration is open; the app learns that from a 403 on submit, no extra endpoint).

| Route | Content | APIs |
| --- | --- | --- |
| `/login` | Username, password, optional account; problem messages; 429 shows "try again in a minute" | `POST /identity/session` |
| `/mowers` | List with live status | `GET /fleet/mowers`, `GET /telemetry/accounts/me/mowers/states`, socket |
| `/mowers/new` | Register a mower: name, connection kind, options (schema form), optional model, serial | `POST /fleet/mowers` |
| `/mowers/[id]` | Detail: status, command bar, blade height and speed, map (Task 11), recent commands | Fleet, Telemetry, socket, map snapshot |
| `/mowers/[id]/settings` | Name, connection kind and options, delete | `PATCH`, `DELETE /fleet/mowers/{id}` |
| `/settings/users` | Sub-users: list, create with role, disable, reset password | Identity `/accounts/{id}/users`, `/roles`, `/role-assignments` |
| `/settings/api-tokens` | List, create (secret shown once), revoke | Identity `/accounts/{id}/api-tokens` |
| `/account` | Profile, password, theme, language, install, notifications (Task 12) | `/me`, `/me/password` |

### 4.1 Mower list

1. Load `GET /api/v1/fleet/mowers` (names, connection kind, site, groups) and `GET /api/v1/telemetry/accounts/me/mowers/states` (`items[{mower_id, state, received_at, stale}]`, `truncated`) in parallel; join by id. Mowers without telemetry show "no data yet".
2. Subscribe all listed ids on the socket (the list owns one subscription handle). States replace store entries.
3. Each row is a `MowerCard`: activity badge, battery readout, RTK fix, connection, last update age (ticks every 10 s locally), error line. Sorted by error first, then mowing, then name. A row is one tap target to the detail page.
4. More than 200 mowers (Realtime's per-socket limit) is a Phase 4 concern; Phase 2 subscribes the first 200 and shows "live updates limited" otherwise.

### 4.2 Mower detail

- **Status panel** (readouts): activity, battery percent and charging, RTK fix and source, connection kind, firmware, blade height, speed, observed time and staleness, error code and message. `raw` is never displayed (the contract says consumers must not rely on it).
- **Command bar.** Start (opens an area picker sheet: areas from the map snapshot as an ordered checklist, and on the map in Task 11; sends `start_mow {area_ids}`), Pause, Resume, Return to dock, and the Emergency stop. Buttons are enabled by activity: Start when docked, charging, idle or paused-and-docked; Pause when mowing or returning; Resume when paused; Dock when mowing, paused or idle away from the dock. Disabled buttons stay visible with a reason on long press, except Start for users without `can_control`, which is not rendered at all.
- **Command lifecycle.** `POST /api/v1/fleet/mowers/{id}/commands` with `CommandRequest {command, ttl_seconds: 30}` returns 202 `CommandAccepted {command_id, expires_at, status_url}`. The command store marks it pending; the socket's `command_result` resolves it (`executed`, `rejected`, `failed`, `expired`, with `reason`). If no result arrives within 8 s or the socket is down, the store polls `status_url` every 2 s until `expires_at`. Results are shown as toasts and in the recent commands list (`GET /fleet/mowers/{id}/commands?limit=10`). Double taps are suppressed while a command of the same kind is pending.
- **Blade height and speed** (`set_blade_height {mm}`, `set_speed {mps}`), available in every activity including mowing (plan: "auch während des Mähens"). `Stepper` controls with ranges from `MowerCapabilities` (`blade_height_range_mm`, `max_speed_mps`; gap G3) and the contract limits as fallback (10 to 120 mm, more than 0 up to 2 m/s). The value is sent on commit (release or 600 ms after the last step), never per tick. The displayed value is the commanded one in a pending style until `MowerState.blade_height_mm` or `speed_mps` confirms it.
- **Map** (Task 11, section 4.3).
- **Settings link** for users with `can_manage_mowers`.

### 4.3 Map (Task 11)

- **Data.** `GET /api/v1/telemetry/mowers/{id}/map` returns the stored `MowerMap` (local metres plus `GeoReference`, `revision`, ETag). Positions come from `MowerState.position` (already WGS84) and from `GET /telemetry/mowers/{id}/positions?from=<job start or today 00:00>&limit=5000` for the driven route.
- **Where conversion happens: in the browser.** The canonical contract is local metres (`maps.py`: "Planning works in metres; only display and telemetry convert"). Converting server-side to GeoJSON would add a second representation to keep in sync and lose the metre frame that spot mowing (Phase 5) edits on the client. The conversion is twelve lines (equirectangular local tangent plane, `mowdeck_control.geo.to_wgs84`). A TypeScript port lives in `libs/mowdeck-ui/src/lib/map/geo.ts`; a Python script writes `libs/mowdeck-contracts/tests/fixtures/geo_vectors.json` (references with rotations 0, 12.5, -90, 359 and latitudes 0, 48, 70, 30 points each), and a Vitest test requires agreement within 1e-9 degrees. The inverse (`from_wgs84`) is ported too, for area selection taps and later editing.
- **Rendering.** Leaflet 1.9.4 (2.0 is still `alpha` on npm), SVG renderer, EPSG:3857. Layers: areas, no-go zones (hatched), obstacles, transfer paths (dashed), dock glyph, mower marker (heading chevron coloured by activity), driven route, selected areas with order badges. Default background is a plain metric grid in the local frame (1 m and 5 m lines) with no tile requests; a satellite or OSM base layer is an opt-in per installation (`PUBLIC_MAP_TILE_URL` in the runtime config, section 9.3), because the OSM tile servers have a usage policy that does not fit a product (https://operations.osmfoundation.org/policies/tiles/) and tiles leak the garden location to a third party.
- **Live marker.** Position updates from the store are applied in a `requestAnimationFrame` loop that interpolates linearly over at most 1 s; heading rotates the shortest way. With `prefers-reduced-motion` the marker jumps.
- **Geoman** (`@geoman-io/leaflet-geoman-free` 2.20.2, peer `leaflet ^1.2.0`) is not needed for display. It is added, lazy-loaded, with the first editing feature (no-go zone edits or spot mowing). Svelte 5 integration is plain: `MowerMap.svelte` creates the Leaflet map in an `$effect` bound to a container element and tears it down in the effect cleanup; layers are updated from `$derived` data. No wrapper library.

### 4.4 Settings, account, theme, language, install

- **Mower settings.** Name, connection kind (`ble_bridge`, `wifi`, `cloud`, `simulated`) and options. Options are an untyped dict in Fleet; a form needs a schema per kind (gap G6, delivered with the adapter in Task 13). Until then the form offers name and kind plus a key/value editor gated behind "advanced". Changing the connection kind shows a warning that the gateway reconnects the mower.
- **Users** (minimal): list members, create a sub-user (username, display name, temporary password with `must_change_password`, one role at account level), disable or enable, reset password. Role editing and per-group assignments come later.
- **API tokens** (minimal): list, create with name and grants (preset: "Home Assistant: view and control, account level"), show the secret once with a copy button, revoke.
- **Theme:** System (default), Light, Dark (section 12.2).
- **Language:** Deutsch (default), English (section 6).
- **Install:** Chromium fires `beforeinstallprompt`; the app stores the event and shows an "Install app" action in the account page and once as a dismissible hint on the list (https://developer.mozilla.org/en-US/docs/Web/API/Window/beforeinstallprompt_event). iOS has no such event; the hint shows the Share, "Zum Home-Bildschirm" instructions instead. Installed state is detected with `matchMedia('(display-mode: standalone)')`.

### 4.5 Notifications (Task 12)

- Settings per user: per mower and per event (finished, stuck or error, battery low with threshold, offline for more than N minutes), quiet hours.
- Web Push: the app asks permission only after a user action ("Enable notifications"), gets the VAPID public key from the server, subscribes through `registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey})` and posts the subscription. iOS and iPadOS deliver Web Push only to web apps added to the home screen (https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/); the page explains that. Web Push requires HTTPS; home installations need TLS on the gateway (for example Tailscale HTTPS certificates), which is a deployment prerequisite to document.
- The service worker handles `push` (shows a notification with title, body, tag = `mower:<id>:<event>` so repeats replace each other, `data.url`) and `notificationclick` (focuses an open client or opens `data.url`). The SW never fetches the API (it has no token); the payload carries everything needed to display.
- Backend: a new Notifications service (plan: "Notifications: Web Push, ntfy, E-Mail") with the endpoints in gap G7.

## 5. Realtime client

### 5.1 Connection manager

`apps/web/src/lib/realtime/client.svelte.ts`, one instance per tab, created lazily by the first subscription.

```ts
type Phase = 'idle' | 'connecting' | 'authenticating' | 'ready' | 'backoff' | 'stopped';

export class RealtimeClient {
  phase = $state<Phase>('idle');
  lastError = $state<string | null>(null);
  #ws: WebSocket | null = null;
  #refs = new Map<string, number>();          // mower id -> ref count
  #attempt = 0;
  #pingTimer?: ReturnType<typeof setInterval>;

  constructor(private deps: {
    url: string;                                // wss://<origin>/api/v1/realtime/ws
    getToken(): Promise<string | null>;         // from session; refreshes if needed
    onState(m: StateMessage): void;
    onResult(m: CommandResultMessage): void;
    onRevoked(ids: string[]): void;
    onSessionLost(): void;
    socketFactory?: (url: string) => WebSocket; // tests inject a fake
  }) {}

  watch(ids: string[]): () => void { /* ref++, subscribe new ids, return release (debounced 2 s) */ }
  reauth(token: string): void { /* send {type:'auth'} on an open socket */ }
  stop(): void { /* logout: close 1000, clear refs */ }
}
```

Protocol handling (ADR 0018, `realtime.py`, README):

| Event | Reaction |
| --- | --- |
| `open` | send `auth` within the 5 s server timeout |
| `ready` | phase `ready`, reset `attempt`, store `token_expires_at` and clock skew from `server_time`, send one `subscribe` with all ref-counted ids (chunks of 200) |
| `subscribed` | mark ids live; `denied` ids: drop them from the store and show "no access" on that page |
| `state` | `onState` (store update) |
| `command_result` | `onResult` (command store) |
| `revoked` | `onRevoked`: remove the mowers from the store and permission cache; a detail page shows "access ended" and returns to the list |
| `error` with `request_id` | resolve that request with an error; `unauthorized` and `limit_exceeded` are followed by a close, handled there |
| `pong` | liveness ok |
| Token refreshed (session event) | `reauth(token)`; server answers `ready` again |
| close 1000 after `stop()` | nothing |
| close 1001, 1006, 1011, 4430 | reconnect with backoff (4430: first retry immediate, slow consumer) |
| close 4401 | get a fresh token (refresh if the current one is within 60 s of expiry or rejected); token available: reconnect at once; refresh failed: `onSessionLost` |
| close 4429 | backoff starting at 30 s, banner "too many open sessions" |
| close 4400, 1009 | client bug: log with connection id, reconnect with backoff |

Backoff: full jitter, `delay = random(0, min(30 s, 0.5 s * 2^attempt))` (https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/). Shortcuts: the `online` event and `visibilitychange` to visible reconnect immediately when in backoff. Application ping every 25 s; no `pong` within 10 s closes the socket (code 4000, client side) and reconnects; this detects half-open sockets after a phone sleeps, which protocol pings cannot report to the page. After a reconnect the server sends the cached state for each subscription, so no REST refetch is needed for states; pending commands are reconciled by polling their `status_url` because results are not cached.

Re-auth timing: the session refreshes the access token 60 s before `exp`; the client sends `auth` with the new token right away. The server closes with 4401 at `token_expires_at` otherwise, so a missed refresh self-heals through the 4401 path.

### 5.2 Subscribe per visible page

```svelte
<!-- routes/mowers/[id]/+page.svelte (excerpt) -->
<script lang="ts">
  import { page } from '$app/state';
  import { realtime } from '#lib/realtime/index.js';
  import { mowers } from '#lib/stores/mowers.svelte.js';
  const id = $derived(page.params.id);
  $effect(() => realtime.watch([id]));          // returns the release function
  const snap = $derived(mowers.get(id));
</script>
```

Release is debounced by 2 s, so navigating list -> detail -> list does not unsubscribe and resubscribe the same mower. When the tab is hidden for more than 5 minutes the client closes the socket (saves battery and the server's per-user limit of 10 sockets); on return it reconnects and resubscribes.

### 5.3 Stores

```ts
// apps/web/src/lib/stores/mowers.svelte.ts
import { SvelteMap } from 'svelte/reactivity';
export interface MowerSnapshot { state: MowerState; receivedAt: number; source: 'rest' | 'live' }

class MowerStore {
  #items = new SvelteMap<string, MowerSnapshot>();
  get(id: string) { return this.#items.get(id); }
  upsert(id: string, state: MowerState, source: MowerSnapshot['source']) {
    const cur = this.#items.get(id);
    if (cur && Date.parse(cur.state.observed_at) > Date.parse(state.observed_at)) return; // never go back in time
    this.#items.set(id, { state, receivedAt: Date.now(), source });
  }
  remove(id: string) { this.#items.delete(id); }
  clear() { this.#items.clear(); }                // on logout
}
export const mowers = new MowerStore();
```

- One entry per mower, newest `observed_at` wins (REST and socket can race).
- Coalescing: the server already replaces unsent states (one slot per mower). In the UI, Svelte batches synchronous updates per microtask; the map marker reads the store in its rAF loop, so a burst of states costs one paint. Readouts that change (battery, activity) get a 300 ms highlight, not an animation per message.
- Staleness: a derived `stale` flag when `now - observed_at` exceeds 3 heartbeats (30 s with the default 10 s heartbeat), shown as a dimmed card with "last seen".
- `commands.svelte.ts`: pending commands by id with status and reason, plus per-mower "pending kinds" for button state.

## 6. Internationalisation

**Choice: Paraglide JS 2** (`@inlang/paraglide-js` 2.26.0, published 2026-10-06; peers `vite >=5`, `typescript >=5.6`). Reasons:

- Messages compile to tree-shakeable functions (`m.nav_mowers()`); a missing key or a wrong parameter is a TypeScript error, so "no hard-coded strings" and "no missing translations" are checked by the type checker rather than at runtime.
- Small bundles: only used messages ship (relevant on phones).
- Strategies fit an SPA: `['localStorage', 'preferredLanguage', 'baseLocale']` with base locale `de` (https://paraglidejs.com/strategy: `localStorage` is browser-only, `preferredLanguage` reads `navigator.languages`). `setLocale()` reloads the document by default; for a language switch in the account page that is acceptable and avoids half-translated state.
- `svelte-i18n` 4.0.1 was last published 2024-10-21 (registry), works with Svelte 5 only through stores, and is untyped key lookup.

Layout:

```
apps/web/project.inlang/settings.json   # baseLocale "de", locales ["de","en"], plugin message format
apps/web/messages/de.json               # {"nav_mowers": "Mäher", "cmd_start": "Starten", ...}
apps/web/messages/en.json
```

Rules:

- Keys are `snake_case`, grouped by prefix (`nav_`, `cmd_`, `status_`, `problem_`, `form_`, `map_`). Enum values map through one function each (`activityLabel(a: MowerActivity)`), so a new enum value in the contracts fails the type check (exhaustive `switch`).
- Numbers, dates and units use `Intl.NumberFormat` and `Intl.DateTimeFormat` with the active locale (decimal comma in German). Units are not translated: `mm`, `m/s`, `%`.
- Lint: a local ESLint rule `mowdeck/no-literal-text` (about 40 lines on the `svelte-eslint-parser` AST) reports `SvelteText` nodes and string literals in `aria-label`, `title`, `placeholder` and `alt` that contain letters; allow-list for units and brand names. No existing rule with this exact behaviour was found (unverified; `eslint-plugin-svelte` 3.23.1 has no i18n rule to my knowledge). `mowdeck-ui` components receive texts as props, so the rule applies to `apps/web` only.
- CI check: both message files have the same key set (`pnpm --filter web i18n:check`).
- Services return English `detail` strings; the app shows its own translation by problem `type` and falls back to `detail`.

## 7. Backend gaps (tasks)

| Id | Gap | Change | Task |
| --- | --- | --- | --- |
| G1 | Login and refresh return `refresh_token` in the JSON body, readable by scripts | Identity: when the request carries `X-Mowdeck-Refresh: cookie-only` (or body field `refresh_transport: "cookie"`), set the cookie and return `refresh_token: null`. Default unchanged for HA and scripts. Tests: body empty, cookie set, refresh works with cookie only | 10 |
| G2 | Refresh reuse detection has no grace; a lost response logs the user out | Identity: accept the immediately previous refresh token of a family for 10 s after its rotation and return the already issued successor (same cookie), audited as `session.refresh_grace`; anything older still revokes the family. Reference: rotation reuse interval in Auth0 (https://auth0.com/docs/secure/tokens/refresh-tokens/configure-refresh-token-rotation, not fetched). Owner decision, see open question 2 | 10 |
| G1b | Traefik's `identity-auth` router rate-limits `PathPrefix(/api/v1/identity/session)` to 10 per minute per IP, which includes refresh and logout | Compose: limit only `Path(/api/v1/identity/session)` (login), `/register` and `/token`; refresh gets its own router with a looser limit (60 per minute, burst 30), since it needs a valid cookie anyway. Matters when several household devices share one IP behind Tailscale or a reverse proxy | 10 |
| G3 | No API exposes `MowerMap` or `MowerCapabilities` (Device-Gateway never calls `get_map()` or `capabilities()`, verified) | See 7.1 | 10 (backend part) and 11 |
| G4 | Module descriptors are not reachable from the browser; `menu_label` is English only | Identity: `GET /api/v1/identity/ui-areas` (authenticated) from the descriptors it already polls, filtered by `perms`; contracts: optional `UiAreaDef.labels: dict[str, str]` | 10 |
| G5 | `/me` shape | Verified: `permissions` (not `perms`), memberships present. Add a contract test in Identity that `MeOut` keeps these fields (the app depends on them) and export it in the OpenAPI | 10 |
| G6 | `connection_options` is an untyped dict, returned in clear in `MowerOut` to every viewer | Contracts: per-kind options models with JSON Schema (`connection_options_schema(kind)`), secret fields marked `writeOnly` and returned masked by Fleet; Fleet `GET /api/v1/fleet/connection-kinds` returns kinds with their option schemas | 13 |
| G7 | No push endpoints | New service `notifications`: `GET /api/v1/notifications/vapid-key`, `POST /subscriptions` (endpoint, keys, user agent label), `DELETE /subscriptions/{id}`, `GET/PUT /preferences`, `POST /test`; consumes `telemetry.mower.*` and command results, derives events (job finished, stuck, battery low, offline), sends with VAPID (RFC 8292). Subscriptions removed on 404/410 from the push service | 12 |
| G8 | Realtime `MOWDECK_REALTIME_ALLOWED_ORIGINS` is empty (all origins) | Compose sets it to the public origin once the app is served at `/` | 10 |
| G9 | No `web` service and no security headers for the app | Section 9 | 10 |
| G10 | Realtime has no map change notification | Optional server message `map_changed {mower_id, revision}` (additive contract change) so an open map refetches | 11 |

### 7.1 Map and capabilities: decision

Options: (a) Fleet `GET /mowers/{id}/map` that proxies a NATS request `device.mower.<id>.map` to the Device-Gateway shard holding the mower; (b) Device-Gateway publishes snapshots, Telemetry stores and serves them.

**Decision: (b).** Reasons: the map is needed when the mower is offline or the gateway restarts (a proxy would answer 503 exactly when the user wants to see where the mower stopped); each page view in (a) would hit the device link, and BLE through the bridge is slow; Telemetry already owns per-mower read models with `can_view` checks and retention; the gateway is sharded, so request routing in (a) needs per-mower core subscriptions on every shard.

Details:

- Contracts: `MowerMapSnapshot {mower_id, account_id, observed_at, map: MowerMap}` and `MowerCapabilitiesSnapshot {mower_id, account_id, observed_at, capabilities}`; subjects `device.mower.<id>.map` and `device.mower.<id>.capabilities`; new stream `DEVICE` (`device.>`, file storage, `max_msgs_per_subject = 1`, `max_age` 30 days, message size up to 1 MB, NATS default max payload). Add to `STREAMS`.
- Device-Gateway: after each connect, fetch `capabilities()` and `get_map()` and publish when the `revision` (or capability hash) differs from the last published one; re-fetch the map after a job ends (zones may have changed in the vendor app). Msg id `<mower_id>:<revision>`.
- Telemetry: durable consumer; tables `mower_maps (mower_id pk, account_id, revision, observed_at, body jsonb)` and `mower_capabilities`; endpoints `GET /api/v1/telemetry/mowers/{id}/map` (ETag = revision, 404 problem `no-map-yet`) and `GET /api/v1/telemetry/mowers/{id}/capabilities`, both `can_view` with hide; deleted with the mower directory entry.
- Because Start needs area ids and the controls need capability ranges, this backend part moves to the beginning of Task 10; Task 11 keeps the visual map, the driven route and `map_changed`.

## 8. Testing and the Phase 2 gate

### 8.1 Unit and component tests (Vitest 5.0.3, jsdom 30, `@testing-library/svelte` 5.4.2)

- `session.svelte.ts`: refresh before expiry with a fake clock; single-flight within a tab; adopt a broadcast token; 401 retry once; failed refresh clears state; logout broadcasts.
- `RealtimeClient` with a fake WebSocket (`socketFactory`): auth on open, subscribe after ready, chunking at 200, ref counting and debounced release, resubscribe after reconnect, backoff sequence (seeded random), reauth on token change, every close code row of section 5.1, ping timeout, `revoked` removes from the store, `denied` handling.
- Stores: newest `observed_at` wins, staleness, command lifecycle including the polling fallback.
- Geo port: golden vectors from Python (section 4.3).
- Components (`mowdeck-ui`): `CommandBar` (buttons per activity, Start hidden without `canControl`, `EmergencyStop` rendered in every combination of props, disabled reasons), `Can` (allowed, fallback, nothing), `StatusBadge` (every `MowerActivity`), `Stepper` (commit on release, bounds from capabilities).
- Svelte 5 component tests need the browser export conditions in Vitest (`resolve.conditions: ['browser']` or the `svelteTesting()` plugin of `@testing-library/svelte`; https://testing-library.com/docs/svelte-testing-library/setup). Coverage target 80 % lines for `lib/auth`, `lib/realtime`, `lib/stores`.

### 8.2 Playwright gate (`tests/e2e-web/`, `@playwright/test` 1.64.0)

Runs against the Compose stack including the new `web` service through Traefik (`MOWDECK_E2E_BASE_URL`, default `http://localhost`), Chromium only in CI (mobile viewport 390x844 and desktop 1280x800 projects), WebKit locally optional. Users and mowers are created per run with a random suffix like `tests/e2e`. Setup through the API (`request` fixture) where the UI is not under test; the login of each role goes through the UI once per worker. Note: storing `storageState` and reusing the refresh cookie across parallel tests would trigger reuse detection, so each test gets its own browser context and logs in (or reuses a per-worker context sequentially). The Identity rate limit (G1b) must allow about 15 logins per run; the suite runs with `workers: 2`.

| Step | Check |
| --- | --- |
| 1 | Owner `lukas-<run>` registers and logs in through the UI; the shell shows "Mäher" (German default) |
| 2 | Owner registers a simulated mower in `/mowers/new`; the list shows it and within 30 s the badge "Angedockt" |
| 3 | Detail page: status readouts filled (battery, RTK, connection `simulated`); area picker lists "Front lawn" and "Back lawn" |
| 4 | Start with "Front lawn": toast "ausgeführt", badge "Mäht" within 30 s without reload (live) |
| 5 | Blade height 50 mm while mowing: command executed, readout shows 50 mm |
| 6 | Return to dock: "Fährt zurück" or "Angedockt" within 60 s |
| 7 | Viewer `vera-<run>` (sub-user, Viewer role): sees the mower, no Start button, the emergency stop is visible and its use is executed |
| 8 | Foreign account `bob-<run>`: empty list; `/mowers/<id>` shows "not found"; no live update arrives |
| 9 | Language switch to English changes the labels; theme toggle sets `data-theme=dark` |
| 10 | Logout: back to `/login`, reload stays logged out (cookie gone) |
| 11 (Task 11) | Map shows areas, dock and a moving marker while mowing; driven route has at least 2 points |
| 12 (Task 13) | Settings: connection kind switch saves and the gateway reconnects (simulated) |

CI job `e2e-web` (`needs: [web, docker]`) reuses the steps of `e2e` (build cached images, `cp .env.example .env`, `docker compose up -d --build --wait`), then runs `pnpm --filter e2e-web test` in the Playwright image `PLAYWRIGHT_IMAGE` (browsers and system libraries prebuilt, host network, see tests/e2e-web/README.md), uploads the HTML report and traces on failure plus `docker compose logs`. Running both suites in one job against one stack would save about two minutes of startup; kept as two jobs for clear failure attribution, revisit if CI minutes matter.

### 8.3 Phase 2 gate text for `docs/phases.md`

> **Phase 2 acceptance criterion.** `docker compose up` serves the web app at `/` next to the API. In the installed PWA (German by default, English available), an owner registers, registers a mower, sees it docked with battery, RTK fix and connection, starts a chosen zone, follows the mower live on the map with its driven route, changes blade height and speed while it mows, pauses, resumes and sends it back to the dock; a Viewer sees status and the emergency stop but no start control; a user of another account sees nothing; push notifications arrive for "finished", "stuck", "battery low" and "offline". All of this is backed by the Playwright suite `tests/e2e-web` (CI job `e2e-web`) against the simulated mower. The adapter package `mowdeck-control-mammotion` is ready for the hardware test: it passes the `MowerConnection` contract tests, the connection kind can be switched in the app, and a documented hardware test checklist exists. The hardware test itself (the household LUBA replaced the Mammotion app for one week) closes the phase.

## 9. Delivery

### 9.1 Dockerfile (`apps/web/Dockerfile`, build context = repository root)

```dockerfile
# syntax=docker/dockerfile:1.7
FROM public.ecr.aws/docker/library/node:22.23.3-alpine AS build
WORKDIR /repo
RUN corepack enable
COPY pnpm-workspace.yaml package.json pnpm-lock.yaml .npmrc ./
COPY apps/web/package.json apps/web/
COPY libs/mowdeck-ui/package.json libs/mowdeck-ui/
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile --filter web...
COPY libs/mowdeck-ui libs/mowdeck-ui
COPY apps/web apps/web
RUN pnpm --filter web build && node apps/web/scripts/csp-hashes.mjs apps/web/build > /repo/csp.conf

FROM public.ecr.aws/nginx/nginx-unprivileged:1.30.5-alpine
COPY apps/web/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /repo/csp.conf /etc/nginx/snippets/csp.conf
COPY --from=build /repo/apps/web/build /usr/share/nginx/html
USER 101
EXPOSE 8080
HEALTHCHECK CMD wget -q --spider http://127.0.0.1:8080/healthz || exit 1
```

`nginx-unprivileged` runs as uid 101 on port 8080 (tags verified on Docker Hub: `1.30.5-alpine` stable, `1.31.6-alpine` mainline). Compose runs it `read_only: true` with `tmpfs: [/tmp]` (the image keeps its temp paths and pid in `/tmp`; unverified for 1.30, check during Task 10). Images for amd64 and arm64 (Raspberry Pi 5 target).

```nginx
# apps/web/nginx/default.conf
server {
  listen 8080;
  root /usr/share/nginx/html;
  server_tokens off;
  include /etc/nginx/snippets/csp.conf;   # add_header Content-Security-Policy "..." always;
  add_header X-Content-Type-Options nosniff always;
  add_header Referrer-Policy same-origin always;
  add_header Permissions-Policy "camera=(), microphone=(), geolocation=(self)" always;
  add_header X-Frame-Options DENY always;

  location = /healthz { access_log off; return 200 "ok"; }
  location /_app/immutable/ { add_header Cache-Control "public, max-age=31536000, immutable" always; try_files $uri =404; }
  location = /service-worker.js { add_header Cache-Control "no-cache" always; }
  location = /manifest.webmanifest { add_header Cache-Control "no-cache" always; default_type application/manifest+json; }
  location / { add_header Cache-Control "no-cache" always; try_files $uri $uri/ /200.html; }
}
```

(`add_header` in a `location` drops the server-level headers in nginx; the real file repeats the security headers per location through an `include`.)

CSP (header from nginx, so `frame-ancestors` works, which a `<meta>` CSP ignores per https://svelte.dev/docs/kit/configuration):

```
default-src 'self'; script-src 'self' <sha256 hashes of SvelteKit's inline bootstrap>;
style-src 'self'; img-src 'self' data: blob: <tile host if configured>; font-src 'self';
connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none';
base-uri 'none'; form-action 'self'; object-src 'none'
```

`connect-src 'self'` covers `wss:` to the same origin in current browsers (CSP Level 3); older Safari needed an explicit `wss://host` (unverified for current versions; the Playwright WebKit run checks it). SvelteKit's `csp: {mode: 'hash'}` hashes its own inline script into a `<meta>` tag for prerendered pages; whether the SPA fallback `200.html` gets it is not documented, so `scripts/csp-hashes.mjs` computes the hashes of every inline `<script>` in the built HTML and writes the nginx snippet; CI fails if a built page has an inline script whose hash is missing. Fonts are self-hosted (no Google Fonts). Leaflet sets positions through the CSSOM (`element.style`), which `style-src 'self'` does not block; inline `style=""` attributes in markup are avoided (unverified for every Leaflet control; the Playwright run collects CSP violations from the console and fails on any).

Runtime configuration without rebuilding: `/config.json` (mounted read-only) with `{ "mapTileUrl": null, "mapAttribution": null, "registrationHint": true }`, fetched at boot. No secrets.

### 9.2 Compose block

```yaml
  web:
    image: mowdeck/web:${MOWDECK_VERSION:-dev}
    build: { context: ../.., dockerfile: apps/web/Dockerfile }
    restart: unless-stopped
    read_only: true
    tmpfs: [/tmp]
    labels:
      - traefik.enable=true
      - traefik.http.routers.web.rule=PathPrefix(`/`)
      - traefik.http.routers.web.entrypoints=web
      - traefik.http.routers.web.priority=1
      - traefik.http.services.web.loadbalancer.server.port=8080
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:8080/healthz"]
      interval: 10s
      timeout: 3s
      retries: 5
    networks: [mowdeck]
```

Priority 1 keeps `/api/v1/*`, `/.well-known/jwks.json` and future routes ahead of the catch-all. Traefik's `depends_on` gains `web`. Realtime gets `MOWDECK_REALTIME_ALLOWED_ORIGINS=${PUBLIC_ORIGIN:-http://localhost}` (G8).

### 9.3 CI jobs

| Job | Steps |
| --- | --- |
| `web` | setup Node from `.node-version`, `corepack enable`, `pnpm install --frozen-lockfile`, `pnpm -r lint` (ESLint + Prettier check + stylelint), `pnpm -r check` (svelte-check 4.7.6, TypeScript 6.0.3), `pnpm -r test` (Vitest with coverage), `pnpm --filter ts-codegen check`, `pnpm --filter web i18n:check`, `pnpm --filter web build`, CSP hash check, bundle size budget (initial JS under 180 kB gzip, map chunk lazy) |
| `docker` | add "Build web image" like the other services (cached, amd64 and arm64 on release) |
| `e2e-web` | section 8.2 |
| `python` | gains `uv run poe openapi --check` |

### 9.4 Conventions

- pnpm 12 via `packageManager` and corepack; `.npmrc` with `engine-strict=true`, `auto-install-peers=false`; Node version in `.node-version` (22.23.3) read by CI and `fnm`/`nvm`.
- ESLint 10 flat config: `@eslint/js`, `typescript-eslint` 8.71.1 (peer `typescript >=4.8.4 <6.1.0`, so TypeScript stays on 6.0.x), `eslint-plugin-svelte` 3.23.1, `eslint-config-prettier`, the local `mowdeck/no-literal-text` rule (https://sveltejs.github.io/eslint-plugin-svelte/). Prettier 3.9 with `prettier-plugin-svelte` 4.1.1. Stylelint 17 with a rule that forbids raw colour values outside `tokens.css` (section 12.9).
- TypeScript 7 (the native port, 7.0.2 on npm) is not used yet: `svelte-check` declares `typescript ^5 || ^6`.
- Licence of `apps/web`: AGPL-3.0-only like the services (it is part of the platform); `libs/mowdeck-ui`: Apache-2.0. Both get LICENSE files and REUSE headers like the Python packages.
- Code, comments and docs in English; UI strings only in `messages/*.json`.

### 9.5 ADR drafts

**ADR 0019: Web app architecture.** Context: ADR 0003 chose SvelteKit; the app needs a module shell, typed API access, and must stay a static, CDN-capable bundle. Decision: SvelteKit 3 SPA with adapter-static and `200.html` fallback, served by an unprivileged nginx image as service `web` at `/` behind Traefik on the API origin; pnpm workspace with `apps/web`, `libs/mowdeck-ui`, `tests/e2e-web`, `tools/ts-codegen`; types generated from committed OpenAPI exports and contracts JSON Schemas with a CI freshness check; Paraglide JS for i18n; native SvelteKit service worker; built-in route modules enabled by `ui_areas` from Identity's aggregation endpoint; map geometry converted in the browser with golden-vector tests; design tokens as CSS custom properties without a utility framework. Consequences: one more toolchain (Node) in CI; no SSR (fine for an authenticated app; no SEO need); SvelteKit 3 is new, fallback to 2.70 documented.

**ADR 0020: Browser authentication flow.** Context: ADR 0015 issues 10 minute access tokens and rotating refresh tokens with reuse detection, and already sets an HttpOnly `SameSite=Strict` cookie on `/api/v1/identity/session`. Decision: access token only in memory; refresh only through the cookie with `X-Requested-With`; the login asks for cookie-only transport so the body carries no refresh token; refresh 60 s before expiry under a Web Lock with BroadcastChannel sharing; one retry on 401; WebSocket re-auth on every refresh; Identity accepts the previous refresh token for a 10 s grace window (if the owner agrees). Consequences: every reload costs one refresh request; XSS can still use the session while the page is open (mitigated by CSP) but cannot exfiltrate the refresh token; the rate limit on refresh must be separate from login.

## 10. Task breakdown

### Task 10: Foundation, list, detail, commands, settings, account, i18n, theme, PWA, gate skeleton

Scope: workspace and tooling (section 9.4), type generation, `mowdeck-ui` tokens and primitives, auth (section 2) with G1, G1b, G5 (and G2 if accepted), module shell with G4, Realtime client and stores, pages of section 4 except the map and notifications, area picker as a list, map and capabilities backend (7.1, G3 backend), i18n de/en, theme, manifest, icons, service worker (precache app shell, offline page, update prompt), `web` image and Compose block, G8, G9, CI jobs `web`, `docker` and `e2e-web` with gate steps 1 to 10, ADR 0019 and 0020, `apps/web/README.md`, docs pages (user guide stub "Die App", developer guide "Frontend").

Definition of done: CI green including `e2e-web`; Lighthouse PWA installability passes on the Compose stack (manual check documented); no CSP violation in the Playwright run; coverage target met; both locales complete; UI review checklist (12.10) done with screenshots in the PR; docs and ADRs merged; `docs/phases.md` progress line.

### Task 11: Map

Scope: `MowerMap` component, geo port with golden vectors, area selection on the map (synchronised with the list picker), driven route from Telemetry positions plus live points, dock and heading, grid background and optional tile layer from `config.json`, `map_changed` (G10), gate step 11.

Definition of done: the simulated garden renders correctly in both themes at phone and desktop size; area selection order is visible and sent as `area_ids` order; the marker follows live states smoothly and respects reduced motion; 1000 route points render without jank on a mid-range phone (manual check on the owner's phone); tests and docs updated.

### Task 12: Notifications

Scope: Notifications service (G7) from `templates/service` with Web Push channel (VAPID keys as secrets), event derivation rules with tests, preferences per user; app page and service worker push handling; iOS home screen guidance; HTTPS deployment note in the operations guide.

Definition of done: on Android Chrome and on an iPhone with the installed PWA the four notification kinds arrive from the simulated mower; a revoked browser subscription (410) is deleted; preferences respected (quiet hours); e2e covers subscription CRUD (delivery itself is checked manually, push services are external).

### Task 13: Adapter and connection switch UI

Scope: integration of `mowdeck-control-mammotion` (built by another agent), per-kind connection option schemas and secret masking (G6), `SchemaForm`, settings page connection switch, capability-driven controls (blade height range, speed max) from the real adapter, gate step 12, hardware test checklist.

Definition of done: switching between `simulated` and `ble_bridge` (and `wifi` when available) in the app reconnects the mower; secret options are never returned in clear; the hardware test checklist is ready for the owner.

## 11. Open questions for the owner

1. **Refresh reuse grace (G2).** Accept a 10 s window in which the previous refresh token returns the already issued successor? It trades a little theft detection sharpness for not logging users out on flaky mobile connections. Recommendation: yes.
2. **Map background.** Grid only (no third party) by default, satellite as opt-in per installation: which provider, if any, for your own installation? A satellite provider usually needs an API key and has terms that must be checked.
3. **HTTPS at home.** Web Push and reliable PWA installation need HTTPS. Is Tailscale (with its HTTPS certificates) the intended access path, or a public domain with Let's Encrypt on Traefik?

---

## 12. Visual direction

Owner decision: **instrument panel**. The app is a cockpit for a machine that is moving in the garden, not a marketing page. It must read in direct sunlight on a phone held in one hand, at a glance, with gloves or wet fingers.

### 12.1 Principles

1. **Colour means state, never decoration.** Neutral surfaces and ink; hue is reserved for mower state, RTK quality, problems and the emergency stop. If something is green, the mower is mowing.
2. **Readouts over prose.** Values are big, monospaced, with tabular figures and units in a smaller weight. Labels are short, uppercase, letter-spaced.
3. **The map is the centre.** On the detail page the map takes the remaining height; status and commands frame it like bezel instruments.
4. **Density with order.** A 4 px grid, hairline dividers instead of cards and shadows, aligned columns. No card grids, no gradients, no hero sections, no illustration, one icon per concept and icons only where they speed up recognition.
5. **Every control is honest.** Disabled states explain themselves, pending states are visible until the mower confirms, nothing animates without a state change.

### 12.2 Theme

Follows `prefers-color-scheme` by default, with a manual override (System, Light, Dark) on the account page, stored in `localStorage` (`mowdeck.theme`). A tiny external script `static/theme-init.js`, loaded synchronously in `<head>`, sets `document.documentElement.dataset.theme` before first paint (external, so no CSP hash needed). Light is the outdoor theme and gets the highest contrast; dark is for evenings and desktops.

### 12.3 Typography

| Role | Font | Size / line height | Weight | Notes |
| --- | --- | --- | --- | --- |
| UI text | IBM Plex Sans (variable, `@fontsource-variable/ibm-plex-sans` 5.3.0) | 16/24 body, 14/20 secondary | 400, 500 | good German glyphs, engineered character without looking generic |
| Labels | IBM Plex Sans | 12/16, `text-transform: uppercase`, `letter-spacing: 0.06em` | 600 | section and readout labels |
| Titles | IBM Plex Sans | 20/28 page title, 24/32 desktop | 600 | no display sizes beyond 32 |
| Readouts | IBM Plex Mono (`@fontsource/ibm-plex-mono` 5.3.0) | 28/32 primary (battery, blade height), 20/24 secondary, 14/20 inline | 500 | `font-variant-numeric: tabular-nums slashed-zero` |
| Units | IBM Plex Mono | 0.6 of the value size | 400 | `48 mm`, `0.40 m/s`, `87 %` |
| Code, ids | IBM Plex Mono | 13/20 | 400 | correlation ids, firmware |

One superfamily keeps the pairing coherent (https://www.ibm.com/plex/, not fetched). Fonts are self-hosted, subset to Latin and Latin Extended, `font-display: swap`, with `system-ui` and `ui-monospace` fallbacks. Type scale tokens: `--md-font-size-12, 14, 16, 20, 24, 28, 32`.

### 12.4 Colour system

Named palettes, tokens per theme. Hex values are the starting point; the contrast test (12.9) is authoritative and may adjust them. Targets: text 4.5:1 or more (7:1 for readouts in light), UI components and status marks 3:1 against their background (WCAG 2.2 SC 1.4.11).

**Neutrals: "Graphite"**

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--md-color-bg` | `#F4F5F2` | `#0E1011` | page |
| `--md-color-surface` | `#FFFFFF` | `#16191B` | panels |
| `--md-color-surface-sunken` | `#E9EBE6` | `#0A0C0D` | readout wells, map frame |
| `--md-color-line` | `#C9CDC4` | `#2A2F33` | hairlines (1 px) |
| `--md-color-line-strong` | `#8C928A` | `#4A5157` | control borders (1.5 px) |
| `--md-color-ink` | `#111412` | `#ECEFEA` | primary text, primary button fill |
| `--md-color-ink-muted` | `#4A514B` | `#A7AEA8` | secondary text, units |
| `--md-color-ink-inverse` | `#FFFFFF` | `#0E1011` | text on ink fills |
| `--md-color-focus` | `#0057D9` | `#7FB0FF` | focus ring (2 px + 2 px offset); the only blue in the UI |

**State: "Field signals"**

| Token | Light | Dark | Meaning |
| --- | --- | --- | --- |
| `--md-state-mowing` | `#1F7A35` | `#56D37A` | mowing |
| `--md-state-returning` | `#0F6E7A` | `#4FC9D6` | returning to dock |
| `--md-state-charging` | `#2F55B8` | `#86A6F2` | charging (with a bolt glyph) |
| `--md-state-docked` | `#4A514B` | `#A7AEA8` | docked, idle (neutral on purpose) |
| `--md-state-paused` | `#9A5B00` | `#F2B24A` | paused |
| `--md-state-error` | `#B3261E` | `#FF7A70` | error, stuck |
| `--md-state-offline` | `#6B716C` | `#7D847E` | offline, always with a dashed outline and "last seen" |
| `--md-state-unknown` | `#6B716C` | `#7D847E` | unknown, with a question glyph |
| `--md-rtk-fixed` / `-float` / `-none` | mowing green / paused amber / error red | same | RTK quality |

Each state has a `-bg` companion at about 12 % tint for badge fills (`--md-state-mowing-bg: #E3F1E6` light, `#12301C` dark). States are never conveyed by colour alone: every badge carries a word and a glyph, offline adds a dashed border.

**Stop: "Signal stop"**

| Token | Light | Dark |
| --- | --- | --- |
| `--md-stop-fill` | `#D0201A` | `#E5352C` |
| `--md-stop-ink` | `#FFFFFF` | `#FFFFFF` |
| `--md-stop-ring` | `#F5C400` | `#F5C400` |

The red on a yellow ring follows the convention of machine emergency stops (red actuator on a yellow background, ISO 13850; https://en.wikipedia.org/wiki/Kill_switch, not fetched). The stop red is reserved for the stop control and differs from the error red so that an error badge never looks like a button.

### 12.5 Spacing, radius, elevation

- Spacing (4 px base): `--md-space-0_5: 2px`, `-1: 4px`, `-2: 8px`, `-3: 12px`, `-4: 16px`, `-6: 24px`, `-8: 32px`, `-12: 48px`. Page gutter 16 px on phones, 24 px from 768 px.
- Radius: `--md-radius-0: 0` (readout wells, map), `-1: 2px` (badges), `-2: 4px` (buttons, inputs), `-3: 8px` (sheets, the stop control's outer ring uses `50%`). No pill buttons.
- Elevation: none for panels (hairlines separate); one shadow token for floating sheets and toasts (`0 8px 24px rgb(0 0 0 / 0.18)`). No glass, no blur.

### 12.6 Touch targets and layout

- Minimum target 48 x 48 CSS px everywhere (WCAG 2.2 SC 2.5.8 requires 24 px, https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html; 48 px suits outdoor use), 8 px minimum gap between adjacent targets.
- Command buttons 56 px high, full width on phones in a two-column grid in the bottom bar (thumb zone). The emergency stop 64 px.
- Phone detail layout, top to bottom: top bar (back, name, connection), status strip (activity badge, battery, RTK, blade height, speed as readouts, horizontally scrollable if needed), map (flex 1), command bar fixed at the bottom with safe-area inset. Desktop from 1024 px: nav rail left, map centre, a 360 px instrument column right with status, controls and recent commands.
- The list on phones: dense rows (72 px) with name, activity badge, battery and age; no cards.

### 12.7 Emergency stop treatment

- Rendered by `EmergencyStop` in the command bar on every mower page and in the list's row action menu, for every user who can view the mower (Viewer included), in every activity, whether or not the socket is connected (it uses HTTP).
- Shape: a 64 px high bar segment with a circular red actuator on a yellow ring at its left, label "NOT-STOPP" / "STOP" in Plex Sans 600 uppercase, `--md-stop-ink` on `--md-stop-fill`. It is the only element using these colours.
- One tap sends `emergency_stop` at once; no confirmation dialog (an accidental stop is harmless, a delayed one is not). Feedback: the button shows "gesendet" within 100 ms locally, then the command result; haptic `navigator.vibrate(30)` where supported.
- It never moves, never hides behind a sheet (sheets stop above the command bar), stays visible with the on-screen keyboard open (the bar is positioned against the visual viewport), and is excluded from `prefers-reduced-motion` changes because it does not animate. A Playwright check asserts it is in the viewport on the detail page at 390 x 844 with the area picker open.

### 12.8 Motion and map styling

Motion:

- Durations `--md-motion-fast: 120ms` (press, toggle), `--md-motion-base: 200ms` (sheet, toast), easing `cubic-bezier(0.2, 0, 0, 1)` (decelerate). Nothing else animates.
- State changes in readouts: a 300 ms background flash in the state tint, then rest. No counters rolling, no skeleton shimmer (missing values show the words "keine Daten").
- Map marker interpolation as in 4.3. `prefers-reduced-motion: reduce` disables flashes, interpolation and sheet slides.

Map styling (fits the panel):

- Background: `--md-color-surface-sunken` with a 1 m grid in `--md-color-line` at 40 % and a 5 m grid at 100 %; scale bar and north arrow in Plex Mono.
- Areas: fill `--md-color-ink` at 6 %, stroke `--md-color-ink` 1.5 px; name label in Plex Sans 12 uppercase at the area centroid.
- No-go zones: 45 degree hatch in `--md-state-error` (SVG pattern), 1.5 px stroke. Obstacles: solid `--md-color-line-strong` at 40 %.
- Transfer paths: dashed `--md-color-ink-muted` 1.5 px (4 4).
- Dock: a square glyph with a notch for the heading, ink fill.
- Mower: a 20 px chevron in the current state colour with a 2 px surface halo; stale or offline: hollow and dashed.
- Driven route: `--md-state-mowing` 2 px, older segments fade to 30 % over the job.
- Selected areas (start picker): stroke `--md-color-focus` 3 px with numbered order badges in Plex Mono.
- Leaflet's default controls are restyled (zoom buttons 48 px, square, hairline borders); attribution in 11 px muted ink.

### 12.9 Encoding

- `libs/mowdeck-ui/src/lib/styles/tokens.css` defines all tokens on `:root` for light, overrides them in `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { ... } }` and again in `:root[data-theme="dark"] { ... }`. A `tokens.ts` file exports the token names as a typed union for the map layer styles (Leaflet needs values: read with `getComputedStyle` once per theme change).
- Components use Svelte scoped styles and only `var(--md-...)`. No Tailwind or other utility framework, no component kit, so no default theme can leak in. A small `base.css` resets and sets typography.
- Stylelint (`stylelint-declaration-strict-value` or an equivalent custom rule, unverified package choice) forbids raw colours, font families and radii outside `tokens.css`.
- `pnpm --filter mowdeck-ui tokens:check` computes contrast ratios of every ink/surface and state/surface pair in both themes and fails below the targets in 12.4.
- A dev-only route `/dev/gallery` (excluded from production builds) renders every `mowdeck-ui` component in every state, both themes, phone and desktop width.

### 12.10 UI review

- Every PR that touches `apps/web` or `libs/mowdeck-ui` attaches screenshots: the Playwright job `ui-screens` captures `/dev/gallery` and the changed pages at 390 x 844 and 1280 x 800 in light and dark and uploads them as an artifact; the PR description embeds the relevant ones. Visual regression with `toHaveScreenshot` on the gallery is added once the design settles (Task 11), to avoid churn during Task 10.
- PR template checklist (UI section):
  - [ ] Only tokens used; no new colours, radii or fonts outside `tokens.css`
  - [ ] Colour is used only for state; every state also has text or a glyph
  - [ ] Readouts in Plex Mono with units; labels uppercase 12 px
  - [ ] Targets at least 48 px, gaps at least 8 px; checked at 390 px width
  - [ ] Emergency stop visible and reachable on every mower view, including with sheets and keyboard open
  - [ ] Light and dark screenshots attached; sunlight check (light theme, full brightness) for new status UI
  - [ ] No hard-coded strings; German and English texts present and fitting (German is longer)
  - [ ] Pending, error, empty, offline and no-permission states designed
  - [ ] Keyboard focus visible, `prefers-reduced-motion` respected

### 12.11 Alternatives considered

**Calm editorial.** Generous white space, a serif display face for mower names, soft neutrals and large photographs of the garden map. Pleasant for a weekly check, but it spends screen height on air, makes live values feel secondary and is weak in sunlight; it suits reports (Phase 4) more than control.

**Bold outdoor gear.** High-visibility orange and black, heavy condensed type, chunky rounded buttons, texture. Very legible outdoors and memorable, but orange collides with warning semantics, the loud chrome competes with the state colours, and it ages quickly. Parts of it survive in the instrument panel: high contrast, large targets, and the yellow ring of the stop control.

## 13. Sources

Versions: npm registry (`https://registry.npmjs.org/<package>`), read 2026-10-09: `@sveltejs/kit` 3.0.1, `@sveltejs/adapter-static` 4.0.0, `svelte` 5.57.2, `vite` 8.3.4, `@sveltejs/vite-plugin-svelte` 7.3.1, `@vite-pwa/sveltekit` 1.1.0 (peer `@sveltejs/kit ^1.3.1 || ^2.0.1`), `vite-plugin-pwa` 2.0.0, `@inlang/paraglide-js` 2.26.0, `svelte-i18n` 4.0.1, `leaflet` 1.9.4 (2.0.0-alpha.1), `@geoman-io/leaflet-geoman-free` 2.20.2, `openapi-typescript` 7.13.0 (peer `typescript ^5.x`), `openapi-fetch` 0.17.0, `json-schema-to-typescript` 16.0.0, `@playwright/test` 1.64.0, `vitest` 5.0.3, `@testing-library/svelte` 5.4.2, `typescript` 6.0.3 / 7.0.2, `typescript-eslint` 8.71.1, `eslint` 10.12.0, `eslint-plugin-svelte` 3.23.1, `prettier` 3.9.9, `prettier-plugin-svelte` 4.1.1, `svelte-check` 4.7.6, `stylelint` 17.16.0, `pnpm` 12.10.1. Docker Hub: `nginxinc/nginx-unprivileged` 1.30.5-alpine, `node` 22.23.3-alpine.

Fetched or searched in this session: https://svelte.dev/blog/sveltekit-3-release-candidate, https://next.svelte.dev/docs/kit/migrating-to-sveltekit-3, https://svelte.dev/docs/kit/single-page-apps, https://svelte.dev/docs/kit/configuration, https://paraglidejs.com/strategy, https://cdn.jsdelivr.net/npm/openapi-typescript@7.13.0/package.json.

Referenced, not fetched in this session: https://svelte.dev/docs/kit/service-workers, https://vite-pwa-org.netlify.app/frameworks/sveltekit, https://openapi-ts.dev/, https://openapi-ts.dev/openapi-fetch/, https://github.com/bcherny/json-schema-to-typescript, https://playwright.dev/docs/auth, https://testing-library.com/docs/svelte-testing-library/setup, https://sveltejs.github.io/eslint-plugin-svelte/, https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html, https://developer.mozilla.org/en-US/docs/Web/API/Web_Locks_API, https://developer.mozilla.org/en-US/docs/Web/API/Window/beforeinstallprompt_event, https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/, https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/, https://auth0.com/docs/secure/tokens/refresh-tokens/configure-refresh-token-rotation, https://operations.osmfoundation.org/policies/tiles/, https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html, https://www.ibm.com/plex/, https://en.wikipedia.org/wiki/Kill_switch.

## 14. Corrections (Tasks 10 to 18)

Implemented in `apps/web`, `libs/mowdeck-ui`, `tools/ts-codegen`, `tests/e2e-web` and the backend changes below (the READMEs there are the reference, [ADR 0020](../adr/0020-web-app-architecture.md) and [ADR 0021](../adr/0021-browser-auth-flow.md) record the decisions, [Web app](web-app.md) is the developer guide). Where the implementation differs from sections 0 to 13, this list wins:

- **SvelteKit 2.70.3 instead of 3.0** (owner decision): `@sveltejs/kit` 2.70.3 with `@sveltejs/adapter-static` 3.0.10 (`fallback: '200.html'`, `ssr = false`), Svelte 5.57.2 with runes, Vite 8.3.4, `@sveltejs/vite-plugin-svelte` 7.3.1, TypeScript 6.0.3. The configuration lives in `svelte.config.js` (not `vite.config.ts`), imports use `$lib` (not `#lib` with extensions) and pages read `$app/state`. `@vite-pwa/sveltekit` is not used either: the native service worker of section 1.2 is kept. Since 2026-10-24 the app runs on SvelteKit 3 as section 1.2 planned (`@sveltejs/kit` 3.0.1, adapter-static 4.0.0, configuration in `vite.config.ts`, `#lib` imports with extensions, the service worker in `src/service-worker/index.ts`); see the addendum of [ADR 0020](../adr/0020-web-app-architecture.md).
- **ADR numbers**: the two ADRs are **0020** (web app architecture) and **0021** (browser authentication flow), not 0019 and 0020; ADR 0019 is taken by the PyMammotion adapter.
- **pnpm 10.34.6** (owner decision) instead of 12, pinned in the root `packageManager` together with its sha512; the Dockerfile enables it with `corepack enable pnpm`, which downloads that version and checks the hash. `.npmrc`: `engine-strict=true`, `auto-install-peers=false`, `save-exact=true`.
- **Node 22**: `.node-version` holds `22` (CI and `fnm`/`nvm` take the newest 22.x; local development ran on 22.20), the root `engines` asks for `>=22.12.0` (`>=22.17.0` since the SvelteKit 3 migration), the Docker build stage uses `public.ecr.aws/docker/library/node:22.23.3-alpine`. Because of that floor, the unit tests use **jsdom 29.1.1**: jsdom 30 needs Node 22.22. Since 2026-10-24 the workspace runs on **Node 24** instead: `.node-version` pins `24.21.0`, `engines` asks for `>=24.21.0`, the build stage uses `node:24.21.0-alpine` (by digest) and `@types/node` is 24.x; jsdom 30 (`^22.22.2 || ^24.15.0 || >=26.0.0`) is satisfied without any engine override ([Web app](web-app.md#node-version) has the pins and the plan for Node 26).
- **`@mowdeck/ui` exports Svelte sources** (`src/lib/index.ts`, the stylesheets and `tokens.ts` through `exports`) without a build step; `@sveltejs/package` is not used. The app's Vite compiles the components.
- **OpenAPI export**: the script is `tools/repo-checks/scripts/export_openapi.py` (poe tasks `openapi` and `openapi-check`), not `tools/export-openapi`. It builds every service in memory (fake bus, store and authorizer) with its real `build_app`, so no `build_openapi_app()` helpers were needed, and writes `apps/web/openapi/<service>.json` with sorted keys, Realtime included. Freshness is enforced by `tools/repo-checks/tests/test_openapi_export.py` (part of `uv run poe test`) and by `uv run poe openapi-check` in the CI job `python`.
- **Generated types**: the Realtime protocol types are written to `apps/web/src/lib/generated/realtime.ts` (from `RealtimeServerMessage.json` and `RealtimeClientMessage.json`), not `contracts.ts`. The REST payloads, including `MowerState`, the map and the capabilities, come from the OpenAPI documents.
- **Telemetry stores both snapshot kinds in one table** `mower_snapshots` (primary key `mower_id, kind`; `account_id`, `observed_at`, `received_at`, `revision`, `body` JSONB; migration `0003`) instead of `mower_maps` and `mower_capabilities`. The durable consumer is `telemetry-device` on `device.mower.>`. The map's ETag is the map revision or, without one, the content hash; `If-None-Match` answers 304. The `DEVICE` stream keeps 256 MiB in total with a duplicate window of 120 s; the gateway's msg id is `<mower_id>:map|capabilities:<sha256 prefix>` (a content fingerprint, so an unchanged map is never published again).
- **G1 is a header only**: login and refresh accept `X-Mowdeck-Refresh: cookie-only`; there is no `refresh_transport` body field. Without the header the body still carries `refresh_token` (scripts, Home Assistant).
- **G2 (refresh reuse grace)**, setting `MOWDECK_IDENTITY_REFRESH_REUSE_GRACE_SECONDS` (default 10, 0 to 60, 0 disables): a rotated refresh token presented again within the grace while its successor was never used counts as a lost response. Identity stores only hashes, so it cannot return the already issued successor; instead it **rotates the unused successor** in its place (new token, same family) and audits `session.refresh_grace`. Only the immediate predecessor of the newest token qualifies; anything else still revokes the family (`session.refresh_reuse`).
- **G1b**: the Traefik router `identity-auth` limits only `Path(/api/v1/identity/session)`, `/register` and `/token` (10 per minute, burst 20); the new router `identity-session` covers `PathPrefix(/api/v1/identity/session/)` (refresh, logout) with its own middleware (60 per minute, burst 30).
- **G4**: `GET /api/v1/identity/ui-areas` returns `module`, `module_version`, `route`, `menu_label`, `labels` and `required_permission`, merged from Identity's own descriptor and the last descriptor every module URL answered with, filtered by the caller's permissions, `Cache-Control: private, max-age=300`. `UiAreaDef.labels` is a map from language tag (`de`, `en-GB`) to label.
- **G8**: `MOWDECK_REALTIME_ALLOWED_ORIGINS` defaults to `http://localhost` in Compose (`REALTIME_ALLOWED_ORIGINS` in `.env.example`), not `${PUBLIC_ORIGIN}`.
- **Map until Task 11** (superseded by the Task 11 list below): the detail page showed the map snapshot as a schematic **`AreaPlan`** (SVG in local metres on a metric grid: areas, no-go zones, dock, selected areas with their order) instead of Leaflet. Leaflet, the geo port with golden vectors, the live marker and the driven route stay in Task 11.
- **Area picker**: a sheet with the areas of the map snapshot; taps select in order and the order is sent as `area_ids`.
- **Token contrast adjustments** made by `tokens:check`: light `--md-state-paused` `#8A5200`, light `--md-state-offline` and `--md-state-unknown` `#5F655F`, dark `--md-color-line-strong` `#6A7279`, dark `--md-stop-fill` `#D0201A` (the same as light, so white stop ink keeps 4.5:1). All other values are as in section 12.4.
- **Gate**: step 4 selects both areas (Front lawn, then Back lawn) so the simulated mower mows long enough for the blade height step 5. The suite runs with one worker; the projects are `phone` (gate steps 1 to 10, 390 x 844), `desktop` (1280 x 800) and `screens`. The settings screenshot is the mower settings page (`apps/web/screenshots/{login,mowers,mower-detail,mower-detail-dark,settings}-{phone,desktop}.png`).
- **Viewer**: a user without `can_control` sees no command buttons at all (not only no Start) and no blade height or speed controls; the emergency stop is always rendered (Fleet checks `can_emergency_stop`).
- **Secret options**: until Task 13 brings per-kind schemas and server-side masking (G6), the settings page treats option keys matching `pass|secret|token|key` as write-only: their values are never shown and only sent when the user enters a new one.
- **Not built in Task 10**: the dev-only `/dev/gallery` route (the runtime `/config.json` came with Task 11); `MowerMap`, `RtkIndicator`, `ConnectionIndicator` and `SchemaForm` (Task 13) do not exist yet, `BatteryGauge` is `BatteryBar` and `Toast` is `ToastRegion`. No prerendered `/offline` page: navigations fall back to the cached `200.html`. Lighthouse installability and the bundle size budget are not automated yet (manual checks). The socket is not closed after 5 minutes in a hidden tab (it reconnects on `visibilitychange` and `online`), there is no haptic feedback on the stop, no account switcher, and `must_change_password` routes to `/account`.

### Task 11 (map)

Implemented in `libs/mowdeck-ui/src/lib/map`, the detail page of `apps/web`, the `web` image and `tests/e2e-web`; [ADR 0023](../adr/0023-map-rendering-and-projection.md) records the decisions, [Web app](web-app.md#map) is the guide. Where it differs from section 4.3, 9.1 and 12.8:

- **Projection**: not always EPSG:3857. Without tiles (the default) the map uses Leaflet's `CRS.Simple` in the mower's local metres, so the 1 m and 5 m grid stays metric and aligned with the mower's frame; the map is then frame-up, and the north arrow is rotated by `rotation_deg`. EPSG:3857 and the WGS84 conversion are used only when tiles are configured. Both modes share the overlay code through a projection adapter (`projection.ts`).
- **Golden vectors**: the script is `libs/mowdeck-ui/scripts/geo_vectors.py` (it imports only `mowdeck_control`) and the fixture `libs/mowdeck-ui/src/tests/fixtures/geo-vectors.json` (not under `libs/mowdeck-contracts`): rotations 0, 12.5, -90 and 359 degrees, origins at latitude 0, 48.1, 70 and -33.9, eight points and five headings each. The test checks within 1e-9 degrees and also the inverse and `compassHeading`.
- **Layer model**: what is drawn is computed by pure functions (`layers.ts`) and unit tested; the Svelte component only maps descriptors to Leaflet layers. Layer styles are CSS classes with tokens on Leaflet's SVG paths (not values read with `getComputedStyle`), so a theme switch needs no redraw; the hatch is an SVG pattern in screen pixels.
- **Driven route**: four age steps (30 %, 50 %, 75 %, 100 % opacity over the shown time span) instead of a continuous fade, a new line after a gap of more than 5 minutes, and an optional RTK tint (fixed, float, none, unknown dashed) with a legend. The window is chosen in a route panel (1 h, today, 24 h; default today, remembered per browser), at most 5000 points from `/positions` plus live points.
- **Marker**: the chevron glides between reports with a 900 ms CSS transition on the marker (none during zoom animation and with `prefers-reduced-motion`), not a rAF interpolation.
- **Controls**: own 48 px controls (zoom, fit, follow, layers, full screen on phones), no Leaflet default controls or attribution box; attribution as an 11 px text line in tile mode only. The map refits on resize until the user pans or zooms.
- **Selection**: the map selects areas whenever Start is possible (owner or operator with `can_control`); Start opens the picker sheet with the map selection already chosen, so the list picker remains and confirms. The emergency stop stays visible in the full-screen map (it covers everything but the command bar).
- **Runtime config**: `/config.json` has the shape `{"mapTiles": null | {"url", "attribution", "maxZoom"}}` (not `mapTileUrl` and `mapAttribution`; no `registrationHint`). It is rendered at container start by `apps/web/nginx/40-mowdeck-runtime-config.sh` from `MOWDECK_WEB_MAP_TILES`, `MOWDECK_WEB_MAP_ATTRIBUTION` and `MOWDECK_WEB_MAP_MAX_ZOOM` (Compose `WEB_MAP_TILES`, `WEB_MAP_ATTRIBUTION`, `WEB_MAP_MAX_ZOOM`) into the tmpfs `/tmp/mowdeck`, together with the CSP whose `img-src` gets the tile origin; nothing is mounted. The variable is `MOWDECK_WEB_MAP_TILES`, not `PUBLIC_MAP_TILE_URL`.
- **Map refresh**: no `map_changed` Realtime message (G10 stays open); the page revalidates the map once a minute with `If-None-Match`.
- **Gate step 11** runs on the phone after step 4: the map shows both areas and the dock, areas are chosen by tapping the map (step 3), the chevron moves between two samples, the route has at least two points, and the full-screen map keeps the stop in view. Screenshots `mower-map-phone`, `mower-map-desktop` and their dark variants were added.

### Tasks 12 and 13 (notifications, connection forms)

Implemented in `apps/web` (`src/lib/push`, `src/lib/notifications`, `src/lib/connection`, the pages `/notifications` and `/notifications/settings`, the service worker), `libs/mowdeck-ui` (`Switch`, the bell icon, `NavRail` badges), Fleet and Notifications; [Web app](web-app.md#notifications-and-web-push) is the guide. Where it differs from sections 3.1, 4.4, 4.5 and 10:

- **Module `notifications` is not `core`**: it follows `mower.view` and the `ui_area` `/notifications` that the Notifications service now announces, so the bell disappears where the service is not deployed (the permission alone decides when `ui-areas` fails). The bell leads to the inbox; the settings are `/notifications/settings`, not a section of `/account`.
- **Unread count by polling**: there is no Realtime message for `notifications.sent`; the inbox store polls every 60 s while the app is open and refreshes on focus and visibility. "Mark as read" happens when an item is opened, plus "mark all read" (one `POST /inbox/{id}/read` per unread item; there is no bulk route).
- **`pushsubscriptionchange`**: the service worker re-subscribes with the VAPID key the page stored in the Cache API (`mowdeck-push`), but cannot post the new subscription because it has no token (ADR 0021). It keeps it in that cache and tells open windows; the page posts it on its next start and deletes the replaced one. A notification click in an open window is routed by the app (`navigate` message) instead of `WindowClient.navigate`, so the in-memory session survives.
- **Settings per user, not per mower and event**: the backend (ADR 0022) has one set of kinds for all followed mowers and a fixed battery threshold and offline delay (server settings), so the page offers kinds, all mowers or a chosen set, one quiet window in the device's time zone, and channels. ntfy and a test notification were added.
- **ntfy token**: `PUT /ntfy` without `token` now keeps the stored token (an empty string removes it), so the write-only field can stay empty on later saves.
- **G6 as a self-describing endpoint**: instead of option schemas in the app, Fleet keeps them as data (`fleet.domain.connections`) and serves them at `GET /api/v1/fleet/connection-kinds`; its validation and the forms use the same schema, and 422 answers carry one field error per option. There is no generic `SchemaForm` in `@mowdeck/ui`: the generated form (`ConnectionForm`) lives in the app because its texts and secret handling are app concerns. Addendum to [ADR 0020](../adr/0020-web-app-architecture.md), consequences: this is the self-describing module idea applied to adapters; a new connection kind needs a schema in Fleet and no UI change (texts fall back to the schema's English).
- **Secrets**: Fleet masks `password` and `noise_psk` (`credential_ref` is a reference and stays visible). The forms never prefill them, show "set" when the server answers `***` and send `***` back to keep the value. The old key/value `OptionsEditor` is gone; options the schema does not describe are listed and kept.
- **Allowed kinds**: `infra/compose/.env.example` now enables `simulated,cloud,ble_bridge`, so the forms of the real adapters are reachable in the Compose stack (the Device-Gateway image contains the adapter); `simulated` alone hides them.
- **Gate**: no new gate step numbers; the specs `notifications.spec.ts` and `connection.spec.ts` run in the `desktop` project next to `desktop.spec.ts`. Web Push delivery is not automated (no push service in headless Chromium); the subscription flow is unit tested with a fake `PushManager`. New screenshots: `notifications`, `inbox` (phone and desktop), `connection-cloud-phone`, `connection-ble-desktop`.

### Task 17 (schedules, RTK signal map; Phase 3)

Not part of the Phase 2 draft above; implemented in `apps/web` (`src/lib/schedules`, the pages under `/schedules`, `SiteLocation`, the detail page), `libs/mowdeck-ui` (`map/rtk.ts`, the RTK layer of `MowerMap`, the icons `calendar`, `clock`, `rain`, `skip`, `pin`, the `date` field) and the Scheduler's module descriptor; [Web app](web-app.md#schedules) is the guide. Decisions that extend sections 3, 4 and 12:

- **Module `schedules` follows `mower.view`**, like the notifications: the Scheduler checks `can_view` per target, and it now announces the `ui_area` `/schedules`. Edit controls are decided per target with `POST /me/permissions` (`can_edit_schedule` and `can_control`; run now `can_control`), so a Viewer sees the list without controls and a schedule opens read-only. `src/lib/live.svelte.ts` caches answers per object (`mower:`, `group:`, `site:`), not only per mower.
- **Rain verdict in five words**: the API's six verdicts collapse to `run` (mowing green), `hold` (paused amber, drop glyph; also the provider-down hold), `unknown` (provider down, the run goes ahead), `no location` (offline tone, dashed) and `rain pause off` (neutral). Run outcomes follow the same language: started green, rain amber, skipped neutral, window closed dashed, mower busy teal, no permission and failed red; every badge has a word and a glyph.
- **Forecast strip** instead of a chart: one bar per forecast hour, height is the probability, hours at a threshold in the paused tone with a drop mark, the threshold as a dashed line, the next firing's window underlined. No chart library.
- **Preview of the saved state**: the editor shows `GET /preview` of the stored schedule and says when the form has unsaved changes; a new schedule shows the current decision at the target mower (`GET /weather/{mower_id}`), its firings after saving. No client-side recurrence engine (the Scheduler's DST rules stay the single source).
- **Validation** mirrors `validate_schedule` and the request models in the client; the Scheduler's domain errors carry no field pointer, so `serverIssues` maps them by their English wording (request errors by pointer).
- **Site location on the mower settings** (not on the account page): the site belongs to the mower, the default site keeps its name, editing needs `can_manage_mowers` on the site.
- **RTK signal map** as a layer of `MowerMap` rather than a page: metric cells in the local frame (0.5, 1, 2 m), worst (default) or majority fix, the RTK tokens at 42 % fill (none at 55 %), unknown dashed; the detail page loads its own range (today, 7 days default, 30 days) in pages of 5000 points up to 20 000 and says when the newest points are all it shows. With the simulated mower every point is `fixed`.
- **Playwright**: `schedules.spec.ts` in the `desktop` project needs the static weather provider with its development endpoint and a 5 s ticker (`tests/e2e-web/stack.env`, merged into the stack's `.env` by CI). New screenshots: `schedules-phone`, `schedule-edit-desktop`, `schedule-runs-phone`, `mower-map-rtk-desktop`.

### Task 18 (joystick, health page, theft alarm acknowledge; Phase 3 acceptance)

Not part of the Phase 2 draft above; implemented in `apps/web` (`src/lib/drive`, `src/lib/health`, the pages `/mowers/[id]/drive`, `/mowers/[id]/health` and `/mowers/[id]/map`, the inbox, the Realtime client), `libs/mowdeck-ui` (`Joystick` and `drive/stick.ts`, the `follow` prop of `MowerMap`, the icons `joystick`, `pulse`, `stop`, `shield`) and `tests/e2e-web`; [Web app](web-app.md#manual-drive) is the guide, [ADR 0026](../adr/0026-manual-drive-channel.md) and [ADR 0027](../adr/0027-health-history-and-theft-alarm.md) the backend decisions. Decisions that extend sections 3, 4, 5 and 12:

- **Drive entry**: the mower page shows "drive manually" when the capabilities say `manual_drive` and `POST /me/permissions` answers `can_drive` (section 3.3 already listed the relation); `drive-sessions` is not asked first. Realtime and Fleet still decide on `drive_start`, and the page shows every refusal code in words.
- **Frames**: the pad samples at the server's `max_rate_hz`; a held, centred stick sends zero frames at 2 Hz only, letting go sends one zero frame and then nothing (the dead-man timer is the backstop, the page adds no motion rule). The Realtime client rate limits frames itself, so excess frames never reach the socket.
- **Blades**: two steps (arm, then switch), only with `manual_drive_blades`, the session opened with `blade_on_allowed`; any stop, error or session end disarms, and the release frame never carries blades.
- **Session end**: leaving the page, a hidden tab and a closed socket end the session; there is no "resume", a new session needs a new start. The screen wake lock is held while driving.
- **Emergency stop on sub-pages**: drive and health pages carry `StopBar`, a command bar with only the emergency stop; `Sheet` now honours `--md-sheet-bottom` on desktops as well, so a sheet stays above it.
- **Health charts** as SVG in tokens (no chart library), like the forecast strip: bar strips for the last 30 UTC days (Telemetry counts UTC days, the page says nothing about local days), the battery line of the last 24 hours with the low band and a marker on the newest value. Durations below an hour read in minutes.
- **Theft alarms in the inbox**: an acknowledge action under alarm items (`POST /inbox/{id}/ack`) and an "acknowledged" badge; the alarm link `/mowers/<id>/map?lat=..&lon=..` (from Notifications) forwards to the mower page, which shows the reported position as a note. No separate map page and no marker for it on `MowerMap`.
- **Playwright**: `drive.spec.ts` and `health.spec.ts` in the `desktop` project. The theft alarm acknowledge is not in the suite: the stack cannot raise an alarm without a long geofence window or a lifted simulator, so it is covered by the Notifications tests and the inbox store tests. New screenshots: `drive-phone`, `drive-desktop-dark`, `health-phone`, `health-desktop`.

### Task 21 (fleet management and reports; Phase 4)

Not part of the Phase 2 draft above; implemented in `apps/web` (`src/lib/fleet`, `src/lib/reports`, the pages `/fleet`, `/fleet/sites`, `/fleet/groups`, `/reports`, role levels on `/settings/users`, placement on the mower settings, sections and quick filters on the mower list), `libs/mowdeck-ui` (`FleetMap` with `map/fleet.ts` and `map/scale.ts`, the "more" sheet of `NavRail`, the icons `fleet`, `chart`, `more`, `site`, `group`, `table`), Telemetry (`GET /reports/summary`, jobs and online time per day), Fleet's and Telemetry's module descriptors and the simulator; [Web app](web-app.md#fleet) is the guide. Decisions that extend sections 3, 4 and 12:

- **Navigation past six modules**: the phone bar keeps five targets (each at least 78 px wide at 390 px); the fifth is "more" and opens a sheet above the bar with the rest, in module order. Grouping modules (e.g. users and API tokens under one entry) was rejected: it changes the desktop rail too and hides a frequently used page one level deeper on every device. The desktop rail shows all eight modules.
- **Two modules, not one**: fleet (map, sites, groups as tabs) and reports. Reports are useful to a household as well and live with Telemetry; the fleet module follows `mower.view` like notifications and schedules, so staff of one group see their mowers on the fleet map. Plan limits (Entitlements) are not part of this task.
- **Fleet map projection**: always `geo` (EPSG:3857). Mowers of several sites have no common local frame, so `CRS.Simple` in one mower's metres does not work; without tiles the map draws its own metric grid around the fleet reference with a 1-2-5 step that follows the visible span (no third party sees the positions). Each mower's areas are drawn in its own frame once its map snapshot is loaded.
- **Markers**: a 48 px target with the chevron or dot of `MowerMap` in the state colour and the name on a tab with a state-coloured edge; selected mowers get a ring. The selected mower's card sits in the side column (under the map on phones), not over the map, so it never hides other markers.
- **Bulk commands** never go out without a confirmation sheet that lists the affected mowers; afterwards the sheet shows Fleet's outcome per mower in words and glyphs (accepted, not allowed, not delivered, not sent). "Everything visible" is one request per visible site; Fleet has no account-wide bulk endpoint, and none was added.
- **Reports in UTC days**, like the health page: Telemetry has no site time zones. Totals in minutes below an hour, availability with one decimal below 10 %. The CSV uses stable English column keys in both languages (machine readable), the dialect only changes separator and decimal mark.
- **Staff per site or group** is Identity's existing role assignment with a level; the users page gained the level picker and per-assignment removal, no backend change.
- **Playwright**: `fleet.spec.ts` in the `desktop` project. The simulator learned `origin_lat` and `origin_lon`, so simulated mowers can stand at different sites. New screenshots: `fleet-map-desktop`, `fleet-map-phone`, `sites-phone`, `reports-desktop`; the older phone screenshots still show the six-item bar and were not regenerated.

### Task 22 (operator console and plan display; Phase 4)

Not part of the Phase 2 draft above; implemented in `apps/web` (`src/lib/plan`, `src/lib/operator`, the components `PlanPanel`, `LimitMeter`, `PlanStatusBadge`, `PlanBanner`, the pages `/operator`, `/operator/accounts/[id]`, `/operator/plans`, `/operator/plans/edit`, `/operator/system`, the plan section of `/account`) and `tests/e2e-web` (`operator.spec.ts`); no backend change. [Web app](web-app.md#plans) is the guide. Decisions that extend sections 3, 4 and 12:

- **Who sees the console**: the operator relation lives only in OpenFGA (`platform:main#operator_admin`), not in the token or `GET /me`, and no module descriptor announces an operator `ui_area`. The shell therefore asks once per session; 200 shows the module (a `core` module with `platformAdmin: true`), anything else hides it. Corrected after the first owner tests: the probe was `GET /identity/platform/accounts?limit=1`, whose 403 for every normal owner showed up as an error in the console; it is now `GET /identity/me/platform` (`{"operator_admin": bool}`), and a 404 or any failure means "not an operator" without logging.
- **One module, three tabs** (accounts, plans, system) with an inverted tab strip, so an operator always sees that they are in the operator area. The console is a section of the household app, not a second app: operator admins are ordinary users with an account of their own, and `libs/mowdeck-ui` serves both.
- **No mower data**: the console calls only Identity's operator list and Entitlements; the system page reads public endpoints for reachability and drops the bodies. Support access (approved, time-limited, logged) stays out of scope.
- **Accounts join** Identity's list with Entitlements' plans by id (same offset first, then single lookups). The search reads Identity's whole list (at most 1600 accounts) because Identity has no search parameter; a server-side search is the step when installations grow beyond that.
- **System health is honest**: `/readyz` is not routed through Traefik, so the page shows reachability and latency from the browser, the billing switch, the plan marked default and counts; no invented metrics. Realtime is shown by this tab's socket only.
- **Plan display for everybody**: the plan section on the account page shows limits as segment gauges like the battery bar (use against limit, amber from 75 %, red at the limit), unlimited as a hatched gauge; with billing disabled a calm note instead of upsell. Plan refusals become words with the limit and the current count or the feature name, as a toast on create actions. Grace and expired show a banner on the mower pages; the emergency stop is never affected.
- **Assignment sheet**: plans as large radio rows (name, key, description), the status as a native select (four German words do not fit a segmented control at 390 px), an optional end date for trial and grace, the reason required before any request.
- **Playwright**: `operator.spec.ts` in the `desktop` project; it grants the operator admin with `docker compose exec identity python -m identity.cli grant-platform-admin` (`MOWDECK_E2E_PLATFORM_ADMIN=off` skips it). New screenshots: `operator-accounts-desktop`, `operator-plan-sheet-phone`, `account-plan-phone`.

### Owner feedback after the first test with the real mower (camera, layout, session)

Corrections to sections 0 to 13 and to the Phase 5 camera design, implemented in `apps/web` and `libs/mowdeck-ui` ([Web app](web-app.md#live-camera) is the reference):

- **Camera inline, no header button**: the camera panel sits in the mower page's side column (desktop) or the content (phone) after the status block and before the areas list (not first; changed after the second test) whenever the mower has a camera, open by default, with a collapse toggle remembered per browser. Collapsed means no session. "Full screen" moved into the panel's head.
- **Side column ends at the command bar**: on the desktop the side column's scroll box ends where the fixed command bar begins (`margin-bottom: var(--md-commandbar-height)`, the measured bar height on the page root); before, the column scrolled underneath the bar. On phones the page scrolls as a whole and its bottom padding (and `scroll-padding-bottom` on the document) keeps content clear of the bar.
- **Only publishing cameras**: the session lists Mammotion's camera slots, which need not exist on the mower (the LUBA 2 AWD X has front left and front right, no rear). The picker lists only uids that publish video, labels them from the slot ids, and a chosen camera without video says so instead of showing another camera.
- **No SDK player**: the Agora SDK's `track.play()` and its autoplay prompt apply inline styles that the CSP refuses; the app attaches the raw media tracks to its own `<video>`.
- **CSP with ports**: Agora's edge servers listen on non-default ports; the runtime CSP adds the Agora hosts with `:*` as well.
- **Hash area names**: an area whose name is only a long number (a vendor hash) reads "Area <last four digits>"; the stored name is unchanged.
- **Session robustness**: pages hung in "loading" after silent refreshes. One refresh per expiry (a 401 for an already replaced token, the socket's 4401 and a tab that waited for the lock take the new token), a 15 s lock timeout, a 10 s refresh timeout, a 30 s deadline per request, 5xx at refresh no longer ends the session, "loading" offers "try again" after 15 s, and the mower list and page keep showing their last data while they reload.

### Owner feedback: too many navigation entries, the desktop page scrolled

Corrections to sections 3.1, 4.4 and 12.6 and to the Task 21 note on grouping modules ([Web app](web-app.md#settings-page-and-desktop-shell) is the reference):

- **Settings grouped after all**: users, AI provider, linked accounts, API tokens, the account page and the notification settings are sections of one settings page (`/settings`) with a section index, instead of six navigation entries; the navigation keeps mowers, fleet, schedules, notifications, reports, settings and the operator console for operator admins. This reverses the Task 21 decision against grouping: on the desktop the rail with ten entries overflowed the window, and the settings pages are rarely used one at a time. Module descriptors and permissions are unchanged (`placement: 'settings'` only moves the module), and the old routes forward to the anchors.
- **No page scroll on the desktop**: the shell is bound to the viewport, the content area is the only scroll container, the rail scrolls inside itself with the brand pinned (the vertical brand mark became a horizontal one) and its tiles shrank from 72 px to 60 px. Phones are unchanged; their "more" sheet now holds reports, settings and the operator console.
