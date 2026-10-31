# ADR 0072: API explorer in the web app, for the public demo

## Status

Accepted (2026-10-30). Amends the follow-up of [ADR 0064](0064-personal-access-tokens-and-public-api-docs.md) ("try it out" in the web app).

## Context

The owner wants the API documentation linked with the public demo ([ADR 0068](0068-public-demo-mode.md)) so that visitors can try the API, not only read about it.

What exists:

- The API reference is plain Markdown generated from the services' OpenAPI 3.1 documents (`apps/web/openapi`, [ADR 0064](0064-personal-access-tokens-and-public-api-docs.md)); it stays the canonical reference, works offline, in the wiki mirror and on GitHub. ADR 0064 named "try it out in the web app" as a follow-up that would reuse the same documents.
- In the demo a visitor's "Try the demo" session already holds a normal 10 minute access token of the shared demo user. Every mutation reaches only simulated mowers or is refused with 403 `demo-mode`; the token exchange, personal access tokens and widget credentials are refused; Traefik's `demo-ratelimit` limits every client address; the demo resets every few hours with its own database and secrets.
- The web app's CSP is strict: `default-src 'self'`, scripts by hash, no inline styles except hashed `style=""` attributes, no third party (`scripts/csp-hashes.mjs`).
- `mowdeck-service-kit` gave every service FastAPI's Swagger UI at `/api/v1/docs`, which loads `swagger-ui-dist` from `cdn.jsdelivr.net`. The gateway never routed it (Traefik sends `/api/v1/<service>` to a service, and the page was at `/api/v1/docs`), so it was reachable only on a service's own port, but it contradicted "nothing from a CDN" ([ADR 0064](0064-personal-access-tokens-and-public-api-docs.md) forces, [docs/dev/web-app.md](../dev/web-app.md)).

Forces:

- No new credential: the explorer must act with the session the visitor already has, never store it, and never need the token exchange the demo refuses.
- No third party: nothing from a CDN, no proxy, no telemetry, no fonts or AI features of the viewer's vendor; a missed setting must not leak anything either.
- The app's CSP must not be weakened for every page because one page embeds a viewer.
- Off everywhere except where it is wanted: on a real installation "try it" changes real data.

## Decision

### Scalar, pinned and self-hosted, in a same-origin frame

The viewer is **Scalar** (`@scalar/api-reference`, MIT), pinned to an exact version in `apps/web/package.json` (`save-exact`, the lockfile holds its integrity, Dependabot bumps it). Its standalone browser bundle is copied from `node_modules` at build time by `build-tools/api-explorer.mjs` into `build/_explorer/` with the frame page (`api-explorer/index.html`), its glue (`explorer.js`, `frame.js`, `explorer.css`), the services' OpenAPI documents with operations under `/api/v1` (Fleet first; Realtime has only its WebSocket) and Scalar's licence. The build refuses a range or a mismatch between the pinned and the installed version, and the page carries the SHA-384 integrity of its scripts and stylesheet. Nothing is checked in and nothing loads from a CDN. `pnpm dev` serves the same files through a Vite plugin.

The web app's page **`/api-explorer`** (route group `(app)`, signed in) shows a short note, links to the scripting guide and the reference, and embeds the frame `/_explorer/index.html`. The frame is its own document, so it has its own CSP and the app's policy stays exactly as it was. `build/_explorer` is written after the app's CSP is computed and outside SvelteKit's build, so it neither widens the app's hashes nor ends up in the service worker's precache.

Considered: Swagger UI and Redoc (Swagger UI's bundle is larger and needs a proxy or its own CSP exceptions too; Redoc has no "try it"), Scalar mounted directly inside the SPA (its runtime `<style>` elements would need `style-src 'unsafe-inline'` for the whole app, or a relaxed policy that stays in effect after leaving the page), a full-page explorer outside the app (it would need its own way to a token: a second refresh flow against the single-use refresh cookie of [ADR 0021](0021-browser-auth-flow.md)).

### The token: handed over by the page, kept in memory

`#lib/api-explorer/bridge.ts` (page) and `api-explorer/frame.js` (frame) talk with `postMessage`, to and from this origin only: the frame posts `mowdeck-explorer-ready`; the page answers with `mowdeck-explorer-token` and the session's current access token, and sends every renewed token (the session refreshes 60 s before expiry) and `null` on logout. The page accepts messages only from the frame's window, the frame only from its parent, and the frame accepts only a JWT-shaped string. The frame adds a `bearer` security scheme to every document, sets this origin as the only server and passes the token to Scalar (`authentication`, `persistAuth: false`), so "try it" calls `/api/v1` on the same origin: no CORS, no proxy. The token never leaves memory.

### Privacy settings

`scalarConfiguration` in `api-explorer/frame.js` (unit tested): `withDefaultFonts: false` (no `fonts.scalar.com`), no `proxyUrl` (never `proxy.scalar.com`), `telemetry: false`, `agent: { disabled: true }`, `mcp: { disabled: true }`, `hideClientButton: true` and `showDeveloperTools: 'never'` (both lead to scalar.com), every `externalUrls` entry (dashboard, registry, proxy, API) pointed at this origin's `/_explorer/offline` (404), `persistAuth: false`. Fonts are MowDeck's IBM Plex when the browser has them, the system fonts otherwise. ~~Scalar's "Generate SDK" button appears only when the page runs on `localhost`; it would open the dashboard address above.~~ *Amended 2026-10-31:* Scalar treats every local URL as local, `127.0.0.1` and LAN addresses included, and there the button opened a dialog that uploads the document to scalar.com and links to scalar.com and x.com. Scalar 1.75 has no option to turn it off; it hides it when the document lists its own SDKs. `prepareDocument` therefore adds `info.x-scalar-sdk-installation` with one entry ("no generated SDK; use the session here and a personal access token in scripts"), so the button and its dialog never appear on any host (unit test and demo e2e spec). The frame's CSP is the backstop: `connect-src`, `img-src`, `font-src` and `script-src` are `'self'`, so a missed setting cannot reach a third party. The demo e2e spec asserts that no request of the page or the frame leaves the origin.

### The frame's CSP

`nginx/security-headers-explorer.conf`, only for `/_explorer/`, differs from the app's in two points:

- `style-src 'self' 'unsafe-inline'`: Scalar injects its stylesheet and its theme as `<style>` elements at runtime. The stylesheet element would take a nonce (`meta[property=csp-nonce]`), but the theme element is rendered by Vue without one, and build-time hashes would break with every Scalar release. Scripts stay `'self'` only: no inline script, no `eval`, no other host.
- `frame-ancestors 'self'` (and `X-Frame-Options: SAMEORIGIN`): the app's page embeds it; nobody else may.

`frame-src 'none'`, `form-action 'none'`, `base-uri 'none'`, `object-src 'none'`. The app's CSP (`frame-ancestors 'none'`, no `'unsafe-inline'`) is untouched; `default-src 'self'` already allows the same-origin frame.

### Demo only: `MOWDECK_WEB_API_EXPLORER`

A runtime switch of the web image like the others (`nginx/40-mowdeck-runtime-config.sh`): `apiExplorer` in `/config.json` and `/tmp/mowdeck/explorer.conf`, which the `/_explorer/` location includes. Off (the default) the page `/api-explorer` is a 404, nothing links to it, and nginx answers 404 for every file of the frame, the Scalar bundle included. `docker-compose.demo.yml` turns it on; Compose (`WEB_API_EXPLORER`) keeps it off everywhere else. On a real installation it can be turned on, and then "try it" acts with the signed-in user's full session and rights; the page says so.

In the demo the banner links it ("API live testen" / "Try the API live"), the sign-in page lists it among what can be tried, the API access settings link it, and the page's note says that every call reaches simulated mowers, that refused actions answer 403 `demo-mode` and when the demo resets. The scripting guide and the reference index point to `/api-explorer` of a demo installation.

### No Swagger UI from a CDN

`create_app` sets `docs_url=None` (and keeps `redoc_url=None`). `/api/v1/openapi.json` stays: the export (`uv run poe openapi`) reads `app.openapi()`, which is unchanged.

## Consequences

- Visitors of the demo can try every endpoint with the session they already have; nothing new to issue, revoke or rate limit, and the demo's policy and Traefik's limit apply as to the app.
- The web image is about 4.6 MB (1.3 MB compressed) larger, served only when the explorer is on, never precached by the service worker.
- A Scalar update is a Dependabot pull request; the frame's CSP and the privacy settings are re-checked by the unit tests (`api-explorer/frame.test.ts`, `build-tools/api-explorer.test.ts`) and the demo e2e spec (`tests/e2e-web/tests/api-explorer-demo.spec.ts`: renders, "try it" GET of the simulated mowers, 403 `demo-mode` for a refused mutation with the same credential, no third-party request, both CSPs). `api-explorer-off-desktop.spec.ts` checks that a regular stack serves none of it.
- The Markdown reference stays canonical; the explorer is a view of the same documents.
- The demo's public address is not decided yet ([ADR 0068](0068-public-demo-mode.md)); the docs name the path `/api-explorer` of a demo installation instead of a host.
