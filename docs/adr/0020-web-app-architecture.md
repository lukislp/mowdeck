# ADR 0020: Web app architecture

## Status

Accepted (2026-10-09). Addendum of 2026-10-24 below: the web app moved to SvelteKit 3.

## Context

[ADR 0003](0003-sveltekit-for-the-web-app.md) chose SvelteKit for the web app: an installable PWA for phones and desktops, German by default with English, organised as a shell with modules, small enough to be served as static files. Phase 2 ([phases](../phases.md)) needs that app on top of the Phase 1 services: login, the mower list and detail with live status, commands, blade height and speed, settings, sub-users and API tokens. The services expose REST APIs with OpenAPI documents and Realtime's WebSocket ([ADR 0018](0018-realtime-websocket-protocol.md)); their module descriptors announce `ui_areas`, but the descriptors are not reachable through Traefik. The app is the first non-Python component of the stack, so its toolchain, its type safety against the Python contracts and its delivery must fit the existing CI and Compose conventions.

The design ([Design: web app](../dev/design-web-app.md), sections 1, 3, 6, 9 and 12) proposed the architecture; this ADR records it as built in Task 10 (section 14 of the design lists the corrections).

## Decision

**SvelteKit 2 as a static SPA.** `@sveltejs/kit` 2.70.3 with Svelte 5 (runes), Vite 8 and `@sveltejs/adapter-static` with the fallback `200.html` and `ssr = false`: every route is client rendered and nothing needs a Node runtime. SvelteKit 3 had been released three days before the task started; the owner chose the mature 2.x line (the module layout works with both, a later migration is mechanical).

**Served by the `web` service at `/`.** An unprivileged nginx image (`nginxinc/nginx-unprivileged`, uid 101, port 8080, read-only root file system with a tmpfs at `/tmp`) behind Traefik with router priority 1, on the same origin as `/api/v1/*`. Same origin means no CORS, the refresh cookie (`SameSite=Strict`, `Path=/api/v1/identity/session`) works unchanged and Realtime's `Origin` check can name one origin. nginx sends the security headers itself (a CSP with the hashes of the inline bootstrap script computed at build time, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, `X-Frame-Options: DENY`, COOP), caches `/_app/immutable/` for a year and revalidates everything else.

**A pnpm workspace beside the uv workspace.** `pnpm-workspace.yaml` lists `apps/*`, `libs/mowdeck-ui`, `tests/e2e-web` and `tools/ts-codegen`; pnpm 10 is pinned in `packageManager`, Node 22 in `.node-version` (Node 24 since 2026-10-24, see [Web app](../dev/web-app.md#node-version)). Playwright and the code generator are separate packages so their dependencies stay out of the app. The root `pyproject.toml` excludes the two JavaScript packages under `libs/` and `tools/` from the uv workspace.

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

## Addendum: migrated to SvelteKit 3 (2026-10-24)

The owner lifted the deferral: the web app runs on `@sveltejs/kit` 3.0.1 with `@sveltejs/adapter-static` 4.0.0 (was 2.70.3 and 3.0.10); Svelte 5.57.2, Vite 8.3.4, `@sveltejs/vite-plugin-svelte` 7.3.1 and TypeScript 6.0.3 already met Kit 3's minimums and stay. Kit 3.1.0 (2026-10-14) was not taken because it was younger than the two weeks the supply-chain rules ask for. The root `engines` rises to Node `>=22.17.0`, Kit 3's floor. The migration started with `sv migrate sveltekit-3` (sv 1.1.1) and was finished by hand:

- The configuration moved from `svelte.config.js` into the `sveltekit({...})` plugin options in `vite.config.ts`, unchanged in substance: adapter-static with `fallback: '200.html'` and `strict`, `serviceWorker.register: false` (the app registers the worker itself, as a module) and `version.pollInterval: 0`. The app stays a static SPA (`ssr = false`, `prerender = false`); the CSP hashes and the nginx image are unchanged in kind.
- `$lib` became `#lib`, declared as `"#lib/*": "./src/lib/*"` under `imports` in `apps/web/package.json`; every import names its file with the extension.
- The service worker moved to `src/service-worker/index.ts` with its own `tsconfig.json` (extends `$app/tsconfig/service-worker`, type-checked by `pnpm check`); `$service-worker` is replaced by `$app/env` (`version`), `$app/manifest` (`immutable`, `assets`, paths relative to the root) and `$app/service-worker` (`self`).
- `goto` options: `replaceState` became `replace`, `noScroll` and `keepFocus` became `reset: false`, and `replaceState(...)` became `goto(..., { shallow: true, replace: true, state })`. Because `goto` now rejects a path without a route, destinations that come from outside the code (`?next=`, notifications, deep links of the native shell, service worker messages) go through `navigatePath`, which loads such a path as a full page so the fallback shows the "not found" page as before.
- `page.url` is read-only; the two places that edit a copy build it from `page.url.href`.
- The `pnpm.overrides` entry that lifted Kit 2's `cookie` to 0.7 (GHSA-pxg6-pf52-xh8x) is gone, Kit 3 depends on `cookie` 2 itself, and the two Kit 2 advisories accepted in `osv-scanner.toml` (GHSA-27mw-82g8-phjj, GHSA-pfhg-q3qv-9q6r, fixed in Kit 3.0.0) are removed. Dependabot no longer ignores the Kit and adapter-static majors.
