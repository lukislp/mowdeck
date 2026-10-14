# ADR 0020: Web app architecture

## Status

Accepted (2026-10-09)

## Context

[ADR 0003](0003-sveltekit-for-the-web-app.md) chose SvelteKit for the web app: an installable PWA for phones and desktops, German by default with English, organised as a shell with modules, small enough to be served as static files. Phase 2 ([phases](../phases.md)) needs that app on top of the Phase 1 services: login, the mower list and detail with live status, commands, blade height and speed, settings, sub-users and API tokens. The services expose REST APIs with OpenAPI documents and Realtime's WebSocket ([ADR 0018](0018-realtime-websocket-protocol.md)); their module descriptors announce `ui_areas`, but the descriptors are not reachable through Traefik. The app is the first non-Python component of the stack, so its toolchain, its type safety against the Python contracts and its delivery must fit the existing CI and Compose conventions.

The design ([Design: web app](../dev/design-web-app.md), sections 1, 3, 6, 9 and 12) proposed the architecture; this ADR records it as built in Task 10 (section 14 of the design lists the corrections).

## Decision

**SvelteKit 2 as a static SPA.** `@sveltejs/kit` 2.70.3 with Svelte 5 (runes), Vite 8 and `@sveltejs/adapter-static` with the fallback `200.html` and `ssr = false`: every route is client rendered and nothing needs a Node runtime. SvelteKit 3 had been released three days before the task started; the owner chose the mature 2.x line (the module layout works with both, a later migration is mechanical).

**Served by the `web` service at `/`.** An unprivileged nginx image (`nginxinc/nginx-unprivileged`, uid 101, port 8080, read-only root file system with a tmpfs at `/tmp`) behind Traefik with router priority 1, on the same origin as `/api/v1/*`. Same origin means no CORS, the refresh cookie (`SameSite=Strict`, `Path=/api/v1/identity/session`) works unchanged and Realtime's `Origin` check can name one origin. nginx sends the security headers itself (a CSP with the hashes of the inline bootstrap script computed at build time, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, `X-Frame-Options: DENY`, COOP), caches `/_app/immutable/` for a year and revalidates everything else.

**A pnpm workspace beside the uv workspace.** `pnpm-workspace.yaml` lists `apps/*`, `libs/mowdeck-ui`, `tests/e2e-web` and `tools/ts-codegen`; pnpm 10 is pinned in `packageManager`, Node 22 in `.node-version`. Playwright and the code generator are separate packages so their dependencies stay out of the app. The root `pyproject.toml` excludes the two JavaScript packages under `libs/` and `tools/` from the uv workspace.

**`libs/mowdeck-ui` as the design system.** A Svelte 5 component library (Apache-2.0) without fetch, global state or i18n runtime (texts arrive as props), exported as sources. Design tokens are CSS custom properties in one file (`tokens.css`) with light and dark themes; there is no utility framework and no component kit. A Stylelint rule allows raw colours, font stacks and radii only in `tokens.css`, and a contrast check fails the tests when a token pair misses its WCAG target. The emergency stop is a component that the command bar always renders.

**Types generated from committed sources.** A Python script exports every service's OpenAPI document in memory to `apps/web/openapi/` (no running stack); `tools/ts-codegen` turns them, plus the Realtime JSON Schemas of `mowdeck-contracts`, into TypeScript (`openapi-typescript`, `json-schema-to-typescript`). The app calls the API with `openapi-fetch`. Both steps have freshness checks in CI, like the contracts' JSON Schemas.

**Built-in route modules, enabled by `ui_areas`.** Each module (`src/lib/modules/<name>/module.ts`) declares route, label, icon, required permission and order. Identity aggregates the `ui_areas` of the descriptors it already polls (`GET /api/v1/identity/ui-areas`, filtered by the caller's permissions). A module is shown when the user holds its permission and either it is core or a deployed service announces its route; if the endpoint fails, the permission alone decides. Remote UI modules (micro-frontends) are out of scope.

**Paraglide JS for i18n.** Messages in `messages/de.json` (base locale) and `messages/en.json` compile to typed functions, so a missing key is a type error; an ESLint rule forbids literal text in the app's markup and a script checks that both locales have the same keys and parameters.

**The native SvelteKit service worker** precaches the shell per version, serves navigations network-first with the cached `200.html` as fallback and never touches `/api/` (it has no token).

**Live state through one socket per tab**, with reference-counted subscriptions, full jitter backoff and re-authentication on every token refresh; per-mower stores in which the newest `observed_at` wins.

## Consequences

- Node joins Python in CI: a `web` job (lint, check, test, codegen check, build), the web image in the `docker` job and an `e2e-web` job with Playwright against the Compose stack.
- No SSR: fine for an authenticated app without SEO needs; the first paint waits for the JavaScript bundle and one refresh request.
- A service that changes a route without regenerating `apps/web/openapi` fails `poe test`; an OpenAPI change without `pnpm codegen` fails the `web` job.
- The menu follows the installation: a stack without Fleet shows no mower module.
- The map is a schematic SVG plan until Task 11 adds Leaflet; geometry stays in local metres on the wire and is converted in the browser.
- Moving to SvelteKit 3 later is a separate, mechanical migration (`$lib` imports, configuration in `vite.config.ts`, `$app/state` already in use).
