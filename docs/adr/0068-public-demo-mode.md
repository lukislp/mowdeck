# ADR 0068: Public demo mode

## Status

Proposed (2026-10-29). Hosting, the reset interval and shared or isolated demo accounts are open decisions of the owner (see "Open questions").

## Context

The owner wants a public demo to link from the portfolio: visitors open it, try MowDeck with simulated mowers and leave. Nobody registers, nothing real is reachable.

What exists:

- The simulator implements the control interface ([ADR 0012](0012-mower-control-interface-and-safety-rules.md)) with its own map, battery, RTK, camera test pattern, Bluetooth bridge, firmware and settings; Fleet's `MOWDECK_ALLOWED_CONNECTION_KINDS` can offer `simulated` only.
- Every service is built with `mowdeck_service_kit.create_app`, which already adds response headers to every response (`X-Source-Code`, AGPL-3.0 section 13).
- Third parties a normal installation talks to: the manufacturer cloud (linked accounts, cloud mowers, the camera over Agora), Bluetooth bridges on the LAN, Web Push services, APNs, ntfy, LLM providers (Vision), Open-Meteo (Scheduler), tile and WMS servers (Telemetry, the web app), GitHub and Apple (sign-in), Stripe (Billing), Matter controllers on the LAN.
- The theft alarm and tracking ([ADR 0027](0027-health-history-and-theft-alarm.md), [ADR 0058](0058-theft-tracking-mode.md)), personal access tokens ([ADR 0064](0064-personal-access-tokens-and-public-api-docs.md)), passkeys, GitHub and Apple sign-in ([ADR 0050](0050-passkeys-and-app-login-hand-off.md) to [ADR 0052](0052-sign-in-with-apple.md)) and the deploy maintenance window ([ADR 0055](0055-deploy-maintenance-window.md)).

Owner requirements (binding):

1. No account creation at all: no registration, invitations, new users, password reset or e-mail. Visitors only enter the prepared demo account through one click, "Try the demo".
2. Visitors may try everything on the simulated mowers (start, pause, dock, manual drive, schedules, zones and angles, maps, health, in-app notifications, heatmaps, settings of the demo data), but nothing that could harm the owner: no real manufacturer accounts or mowers, no outbound traffic to third parties, no API tokens, no changes to identity, security or billing, no access to the owner's real installation, rate limits against abuse, nothing persistent beyond the next reset.
3. The demo is labelled everywhere and the labels cannot be dismissed: a banner on every page (phones and the app shells included), "Demo" in the page title, a demo badge on the sign-in entry, and `X-MowDeck-Demo: 1` on API responses.

## Decision

### One switch, enforced by every service

`MOWDECK_DEMO_MODE=true` is a setting of the service kit (`ServiceSettings.demo_mode`). With it `create_app` installs `DemoModeMiddleware` (`mowdeck_service_kit.demo`):

- every HTTP response carries `X-MowDeck-Demo: 1`, errors and health checks included;
- before any route runs, a mutating request (`POST`, `PUT`, `PATCH`, `DELETE`) that the service's `DemoPolicy` names is answered `403` with the problem type `urn:mowdeck:problem:demo-mode` and a sentence for the visitor. Safe methods always pass.

A policy is either a list of refused paths (most services: the visitor may try everything but the few actions that reach out) or, with `allow_only`, a list of the only allowed mutations (Identity, Entitlements, Billing: a new endpoint stays closed until someone decides it is safe). The web app shows the problem as "Not available in the demo".

What each service refuses, and what it forces regardless of its variables:

| Service | Refused in demo mode | Forced in demo mode |
| --- | --- | --- |
| Identity | everything but `POST /demo/session`, `POST /demo/seed` (seed secret), refresh, logout, the permission check and the setup tour: registration, password, passkey, GitHub and Apple sign-in, the app hand-off, token exchange, personal access tokens, widget credentials, password and passkey changes, step-up, logout everywhere, the security center's alert switch and ending sessions ([ADR 0065](0065-security-center-and-new-login-alerts.md); its overview, sessions and events stay readable), account, users, roles, API tokens, platform | registration `closed`, passkeys, GitHub and Apple off; demo sign-ins remember no device and raise no new-login alert |
| Fleet | linking manufacturer accounts, adding, renaming, reconfiguring or removing mowers, the Bluetooth bridge link, adding or removing base stations | `allowed_connection_kinds = {simulated}` |
| Device-Gateway | (no API) | only simulated connections are created (`SimulatedOnlyError` for any other kind), no cloud factory, no cloud discovery |
| Notifications | Web Push subscriptions, APNs devices, Live Activities, ntfy, the test message | no channel at all (the inbox only), no ntfy servers, no push photos |
| Vision | an own AI provider (set, test, remove), picture uploads | installation default on with the rule based `fake/recover`, users may set no base URL |
| Scheduler | the development weather endpoint | static weather (no Open-Meteo), no development endpoints, at most 30 schedules |
| Entitlements, Billing | every change | |
| Telemetry, Realtime, Planner | nothing; labelled only | |

Commands, manual driving, spot runs, the camera test pattern, firmware updates of the simulator, area names, mowing defaults, sites and groups, schedules, angles, the inbox, the theft tracking of a simulated mower and the fake AI assistant all work: they only touch simulated mowers and the demo's own database.

### "Try the demo" instead of accounts

Identity gets `POST /api/v1/identity/demo/session`: a session (the same tokens and refresh cookie as a password login, `amr` `demo`) of the prepared user `MOWDECK_IDENTITY_DEMO_USERNAME` (default `demo`), without credentials. It is `404` outside demo mode and before the seed, and has the per-address login limit (in Identity and in Traefik). The user has a random password nobody knows; password login is refused anyway. Every visitor shares this one account (see "Open questions").

### Seeding

A one-shot Compose service `demo-seed` (`infra/compose/demo/seed.py`, standard library only, in the identity image) prepares a fresh demo before the Device-Gateway starts:

1. `POST /identity/demo/seed` creates the demo account and user (idempotent).
2. `POST /identity/demo/session`, then the setup tour is marked done.
3. Fleet registers three simulated mowers in three gardens: `family` (lawn with pond and apple tree, side strip, Bluetooth bridge), `orchard` (a large meadow with trees and beehives, front lawn, Bluetooth bridge) and `default` (two lawns, a firmware update on offer). The simulator got the option `garden` for this (`mowdeck_control.simulator.GARDENS`).
4. The Scheduler gets a weekly schedule per mower with angle rotation.
5. `POST /telemetry/demo/history` simulates the past days offline (`mowdeck_control.history.simulated_history`: one job per day over every area, from the dock back to the dock, with Wi-Fi falling off with the distance from the house, a varying mobile signal and RTK float near the first no-go zone) and feeds them through Telemetry's normal ingestion, so tracks, coverage, heatmaps and the health history exist from the first minute. The history goes in before any live state exists, which is why the Device-Gateway waits for the seed job.

Calls the policy refuses to visitors (registering mowers, the seed endpoints) carry the shared secret `MOWDECK_DEMO_SEED_TOKEN` in `X-MowDeck-Demo-Seed`. The seed job talks to the services inside the Compose network only; Traefik removes that header from every outside request, and the seed endpoints compare it in constant time and are `404` outside demo mode.

### The demo stack

`infra/compose/docker-compose.demo.yml` on top of `docker-compose.yml`, as its own Compose project (`mowdeck-demo`) with its own database, volumes, network and secrets (`infra/compose/demo/demo.env`, from `demo.env.example`); it never shares a stack, database or secret with an installation with real mowers. It:

- sets `MOWDECK_DEMO_MODE=true` on every service and the forced values above;
- adds two Traefik entry point middlewares: `demo-headers` (labels every response, the web app included, and empties `X-MowDeck-Demo-Seed`) and `demo-ratelimit` (per client address, default 1200 requests a minute, burst 600, the web app's files included); "Try the demo" also has Identity's login limit;
- runs the seed job and the live simulation a little faster than real time (`DEMO_SIM_TIME_SCALE`, default 3);
- leaves map tiles, aerial photos, the street map, the camera relay, Agora, Matter and Billing off. `demo/reset.sh` refuses `COMPOSE_PROFILES`;
- publishes only the gateway's HTTP entry point (`GATEWAY_HTTP_PORT`): no HTTPS port, and no host port of NATS, Postgres or OpenFGA (amendment below).

### Periodic reset

`infra/compose/demo/reset.sh` deletes the demo project with its volumes and starts it again (`down --volumes`, `up --build --wait`); the seed job runs on every start. A systemd timer or cron entry runs it every `DEMO_RESET_HOURS` (default 6; docs/ops/demo.md). Nothing a visitor does survives a reset. The script only touches the project named in `demo.env`, refuses the project `mowdeck` and the placeholder secrets.

### Labels in the web app

`MOWDECK_WEB_DEMO_MODE` and `MOWDECK_WEB_DEMO_RESET_HOURS` add `demo: { resetHours }` to `/config.json`. The app then shows:

- a band above every page ("Demo · Simulated mowers, shared with every visitor. Resets every 6 h.", short on phones) in the strip the page frame reserves at the top, with a hatched amber tag like a test rig's calibration tape and a link to the source code. It has no close button and is part of every page, so it also shows in the iOS and Android app shells, which load these pages;
- "Demo · " in front of every page title (kept there while pages change their title);
- on the sign-in page only the demo entry: a demo badge, what can be tried, the "Try the demo" button and the note that the demo is shared and reset; no password form, no other sign-in method, no link to registration;
- on the registration page a note instead of the form.

The labels in the web app are a courtesy; the enforcement is in the services.

## Consequences

- A demo is one environment variable per service plus an override file. A new mutating endpoint is refused in Identity, Entitlements and Billing until it is added to their allow lists; in the other services it is allowed until someone adds it to the policy, which a review of every new endpoint must consider (the table above is the checklist).
- The seed secret is the one way past the policy. It only matters inside the demo's own network: Traefik strips it, and a demo has no real data to protect behind it.
- All visitors share one account: one visitor can pause another's mower or delete a schedule. The reset bounds the damage; the policy keeps the mowers themselves and the account intact.
- Unit tests cover the middleware, every service's policy and forced settings, the seed endpoints, the gardens and the history; the Playwright specs `demo.spec.ts` (projects `demo`, `demo-phone`) run in the CI job `e2e-demo` against the test stack with the demo override.
- Nothing is deployed by this change; where and under which address the demo runs is the owner's decision.

## Amendment (2026-10-31): hardening after the network audit

A network audit of a local demo found three gaps; the decision above is unchanged, the stack is tightened:

- **IPv6 per /64.** Per-address limits keyed the full IPv6 address, so one connection (a /64) had unlimited buckets. Every Traefik rate limit (Compose and Helm) now sets `sourceCriterion.ipStrategy.ipv6Subnet=64`, and the in-process limiters (Identity's login throttle, Telemetry's public tracking limits) key `mowdeck_service_kit.client_address.limiter_key`: IPv4 unchanged, IPv6 its /64. The client address itself stays whole for audit logs and the security center. Telemetry's limiter no longer clears every counter when it holds 10,000 addresses; it forgets the least recently active ones.
- **Host ports.** The demo published NATS (client and monitor), Postgres and OpenFGA on host loopback and an HTTPS port no router used. `docker-compose.demo.yml` now drops them (`ports: !reset []`, Traefik `ports: !override`); the regular stack is unchanged. NATS has no authentication in any stack; authenticating the services to NATS (one credential per service) is a follow-up with its own ADR.
- **Override allow-list.** `demo/reset.sh` compared a deny-list of lines, which a new router, Traefik API flags, `MOWDECK_CLIENT_IP_DEPTH`, ports or an image swap got around. It now diffs `docker compose config --format json` with and without `demo/demo.override.yml` (`demo/override_guard.py`, needs `python3`) and accepts only networks of existing services, new external networks, resource limits and logging.

## Open questions

- **Hosting**: which host and address (a small VPS, a separate VM; never the host of a real installation), TLS in front of Traefik (`GATEWAY_TRUSTED_PROXIES`), and whether a public demo in Germany needs the imprint pages (`WEB_LEGAL_ENABLED`, filled `/legal/*.md`).
- **Reset interval**: 6 hours by default; shorter resets limit vandalism, longer ones keep a visitor's session alive longer (a reset ends every session).
- **Shared or isolated accounts**: one shared demo account (this ADR) is simple and shows the multi-user live view, but visitors see each other's actions. An account per visitor would need a pool of seeded accounts or seeding on demand (about a second of simulated history per mower), plus a cap on concurrent accounts.
