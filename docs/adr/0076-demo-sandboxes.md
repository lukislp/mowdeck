# ADR 0076: Demo sandboxes, one account per visitor

## Status

Accepted (2026-11-01). Extends [ADR 0068](0068-public-demo-mode.md) (public demo mode) and answers its open question "shared or isolated accounts"; changes where [ADR 0073](0073-guided-tour.md) stores the tour's outcome in the demo.

## Context

The public demo ([ADR 0068](0068-public-demo-mode.md)) signs every visitor in to one shared demo user with three shared simulated mowers. Visitors see each other's commands live, one visitor can pause another's mower or delete a schedule, and every read endpoint answers with the shared user's rights, which ADR 0068's amendments had to filter (sessions, audit log, sockets per login session). The guided tour had to remember "seen" per browser, because a server-side outcome of the shared user would end the tour for everyone.

The owner wants every visitor in an own, isolated sandbox: an own account with its own simulated mowers, zones, schedules and notifications, created when they click "Try the demo", reachable only from their browser session, invisible to everyone else. The demo runs on a small VPS (2 vCPU, 7.8 GB) and is reset every 6 hours.

Requirements: creation within about 2 seconds; idle and absolute lifetime with a complete, idempotent cleanup that survives a service being down; a cap on concurrent sandboxes derived from measurements, a per-address creation limit and a friendly "demo is full" answer; cheap simulated mowers; the tour once per visitor; nothing changes outside demo mode; provider neutral.

## Decision

### A sandbox is an ordinary account

A sandbox is a normal account of Identity with one owner user (`visitor-<random>`, no password, no e-mail), the default roles, the owner's OpenFGA tuples and the default plan, created through the same `AccountService` path as a registration (without hashing a password; `AccountService.create`). Fleet, Scheduler, Notifications, Telemetry, Vision and Planner need no notion of a sandbox: each sandbox is just another account, so the existing per-account isolation (OpenFGA, the account check of every query, Realtime's account check on every frame) separates visitors. No new authorization model, no read filter. The shared-user filters of ADR 0068 stay for the shared mode.

Identity orchestrates (`identity.application.sandboxes.SandboxService`): it owns the accounts and the login sessions, has a database for the sandbox registry (`demo_sandboxes`, migration 0010) and already runs background jobs. The other services are reached through a port (`SandboxProvisioner`); its adapter (`identity.adapters.demo_sandbox.HttpSandboxProvisioner`) calls their HTTP APIs inside the Compose network, as the sandbox user (a provisioning access token without a login session) and, where the demo policy refuses visitors (registering mowers, the history, the purges), with the demo seed secret, which Traefik strips from every outside request. Nothing of this is wired outside demo mode with `MOWDECK_IDENTITY_DEMO_SANDBOXES`.

### A warm pool, claimed in one click

Filling a sandbox means three mowers in Fleet (the Device-Gateway starts their simulators on `mower.registered`), a schedule per mower, the notification preferences and a week of simulated history in Telemetry. The history is the slow part: about 6,700 states, each one Telemetry transaction, tens of seconds on the VPS. A click must not wait for it, and Telemetry refuses states older than the latest one, so the history has to be in before any live state.

So Identity keeps a **pool** of ready sandboxes (`DEMO_SANDBOX_POOL`, default 2), filled one at a time in the background, starting once the other services answer `/readyz`. Their mowers rest from the start (below), so no live state can overtake the history. "Try the demo" (`POST /identity/demo/session`, unchanged for the web app) claims the oldest ready sandbox and starts a login session of its user: the same tokens and refresh cookie as any sign-in, `amr` `demo`, no device remembered. That takes a few database rows. The claim asks the pool to refill. With an empty pool (a burst of visitors) a sandbox is made on the spot without history, which takes about a second; its tracks and heatmaps fill as the visitor mows.

### Limits

- **Cap.** Claims are serialised; active sandboxes plus the ones being made for a visitor are capped at `DEMO_SANDBOX_MAX` (default 40, from the measurements below). One more visitor gets `503` with the problem type `urn:mowdeck:problem:demo-full` and `Retry-After` (120 s). Pool sandboxes do not count: they rest.
- **Per address.** One client address (IPv6: its /64, `limiter_key`) may start `DEMO_SANDBOX_PER_ADDRESS_PER_HOUR` sandboxes an hour (default 6; `429` with `Retry-After`), on top of Traefik's login limit on the route.
- **Full page, not a shared fallback.** The web app answers `demo-full` with a page in the demo entry: "the demo is full", a countdown from `Retry-After` and an automatic retry, plus "try again now". A shared read-only view was rejected: it would bring back the shared account this ADR removes, need a read-only principal and a second mode in every page, and the full state is rare and short (sandboxes of visitors who left are deleted within the idle time).

### Resting mowers: an idle sandbox costs only rows

Realtime knows who watches which account. In demo mode it announces on the core NATS subject `presence.accounts` (`AccountPresence`: instance, watchers per account) when the first socket of an account opens, when the last one closes, and every 30 s as a keep-alive with every watched account. The Device-Gateway (demo mode, `MOWDECK_SIM_PAUSE_UNWATCHED`, default on) keeps a `PresenceTracker`: an account is awake while some Realtime instance reported a watcher within 2.5 keep-alive intervals (a dead instance stops counting by itself), or for `MOWDECK_SIM_WAKE_SECONDS` (120) after a request reached one of its mowers (a command over the API, a scheduled start, a photo, a path preview). The mowers of a sleeping account **rest**: the session keeps the connection but cancels the simulation clock and sends no state and no heartbeat; simulated time stands still. On waking, the clock restarts where it stopped and the current state goes out at once. A command to a resting mower wakes it and is executed. Outside demo mode nothing announces presence and every mower always runs. The NATS subject map (ADR 0074) lets Realtime publish and the Device-Gateway subscribe to `presence.accounts`.

### Lifetime and cleanup

The sweep (every minute) sends a sandbox to `purging` when

- its user's newest login session row is older than `DEMO_SANDBOX_IDLE_MINUTES` (default 30): every refresh writes a row, and the web app refreshes every few minutes while a tab is open, so a closed tab is idle after 30 minutes, an open one never;
- its login session ended (the visitor logged out, or a refresh token was reused);
- it was claimed more than `DEMO_RESET_HOURS` ago (the absolute limit, the reset interval by default);
- it has been stuck in `provisioning` for 15 minutes.

Its sessions are revoked at once. The purge then calls `DELETE /api/v1/<service>/demo/accounts/{id}` (seed secret, 404 outside demo mode, hidden from the API documents, `DemoPurgeOut`) in every service, Fleet first: removing the mowers publishes `mower.removed`, which stops their simulators. The other services follow after 30 s, so the mowers' last states have landed and are deleted with the rest. Each endpoint deletes every row of the account (and, in Fleet, the mower, site and group tuples; Fleet's and Identity's append-only audit triggers let a purge `DELETE` through for one transaction, `mowdeck.audit_purge`), is idempotent and answers how many rows it removed. Identity then deletes its own rows (sessions, memberships, roles, the user, the audit rows, the account) and the user's and the platform tuples, then the registry row. A failing service is retried with backoff (15 s doubling, at most 10 minutes); `purged` remembers which services confirmed, so a retry skips them. No service holds NATS consumers per account, so there is nothing to remove on the bus.

The 6-hourly full reset stays the safety net behind all of this. After a reset (or an idle deletion) the visitor's refresh cookie no longer works; the web app remembers that this browser was a demo visitor (`localStorage` `mowdeck.demo.visitor`) and the sign-in page then starts a fresh sandbox by itself ("Preparing a fresh sandbox…"), without an error page. An explicit logout clears the flag.

### Tour

In a sandbox the tour's outcome goes back to the user's own storage (Identity's `dismissed_hints`, ADR 0073), and the browser keeps `mowdeck.tour.demo` as well: a returning visitor whose new sandbox is fresh does not get the tour again (the browser's outcome wins when loading). The shared mode keeps the browser-only outcome.

### Settings

All in `docker-compose.demo.yml` with defaults, so `demo/demo.env` needs no new key:

| Variable (demo.env, optional) | Default | Setting |
| --- | --- | --- |
| `DEMO_SANDBOXES` | `true` | `MOWDECK_IDENTITY_DEMO_SANDBOXES`, `MOWDECK_WEB_DEMO_SANDBOX`; `false` brings back the shared demo user |
| `DEMO_SANDBOX_MAX` | `40` | Concurrent sandboxes |
| `DEMO_SANDBOX_POOL` | `2` | Ready sandboxes |
| `DEMO_SANDBOX_IDLE_MINUTES` | `30` | Idle lifetime (also shown in the band) |
| `DEMO_SANDBOX_PER_ADDRESS_PER_HOUR` | `6` | Sandboxes per client address and hour |
| `DEMO_RESET_HOURS` | `6` | Also the absolute lifetime of a sandbox |
| `DEMO_HISTORY_DAYS` | `7` | Days of history of a pool sandbox |

## Measurements

Method: `tools/repo-checks/scripts/demo_sandbox_cost.py` against the e2e demo stack (`MOWDECK_E2E_DEMO=1`, every service of the demo, time scale 3, the e2e fakes beside it), on a desktop host under load from another stack. Three phases of 60 s each, sampled with `docker stats` every 2 s: the baseline, N visitors who clicked "Try the demo" and left (no socket), and the same N visitors with a socket each, subscribed to their three mowers, one of them mowing. CPU is in percent of one host core (sum over every container), memory the sum of the containers' resident memory. Numbers from a VPS vCPU will differ; rerun the script on the target host before raising the cap.

| | 20 sandboxes | 40 sandboxes |
| --- | --- | --- |
| "Try the demo", pool empty (made on the spot) | | 0.3 to 0.96 s (median 0.7 s) |
| Idle sandbox (visitor left, mowers resting) | 0.06 % CPU, 1.0 MiB | about 1 % CPU (the pool refilling with history during the phase), under 1 MiB |
| Watched sandbox, one mower mowing | 5.1 % CPU, 2.2 MiB | 8.1 % CPU, 1.1 MiB |
| Whole stack while watched | +101 % CPU | +322 % CPU |

The cost of a watched sandbox is not the simulator (the Device-Gateway is about a tenth of it) but the telemetry its three mowers send and what consumes it: Telemetry's ingestion, Notifications' watchers, the Scheduler, Postgres and NATS. Above about 30 watched sandboxes the host saturated, so the cost per sandbox rose. A resting sandbox costs nothing measurable: no simulation, no state, only its rows (a pool sandbox with a week of history about 7,000 rows in Telemetry).

**Default cap.** A 2-vCPU VPS has about 200 % to give. Keeping a third free for the stack's own baseline and headroom leaves room for about 20 to 25 sandboxes that are watched at the same time. Sandboxes whose visitor closed the tab or put the phone away rest and cost nothing until they are deleted 30 minutes later, so the cap counts claimed sandboxes, not watched ones: `DEMO_SANDBOX_MAX=40` assumes that about half of them are watched at any moment. When all 40 are watched the mowers update more slowly, nothing fails. An operator who wants headroom for 40 watched visitors at once sets `DEMO_SANDBOX_MAX=25` or gives the VPS more cores. Memory is no limit (a few MiB per sandbox on 7.8 GB).

## Consequences

- Visitors never see each other: separate accounts, checked by OpenFGA and every service's account filter as for any installation with several households. The read filters of ADR 0068 matter only in the shared mode.
- A new service that stores per-account data needs a demo purge endpoint and an entry in `PURGE_ORDER` (`identity.adapters.demo_sandbox`); otherwise its rows stay until the next reset. This is the checklist item for new services in a demo.
- The sandbox garden is the shared demo's garden (`infra/compose/demo/seed.py`); a test keeps both lists equal.
- A burst beyond the pool gets sandboxes without history; the pool size trades idle database rows for that.
- Tests: Identity (`test_identity_demo_sandboxes.py`: pool, claim, isolation of two visitors, cap, per-address limit, idle, logout, expiry, stuck, a failed purge resumed; `test_identity_demo_provisioner.py`), the purge endpoint of every service (`test_*_demo_purge.py`, memory and Postgres), Realtime (`test_realtime_presence.py`: announcements, keep-alive, nothing outside demo mode, two visitors over the socket), the Device-Gateway (`test_dg_presence.py`: the tracker, resting and waking mowers, a command waking a mower, outside demo mode), the web app (sandbox helpers, the sign-in page's demo entry, the tour stores) and Playwright (`sandbox-demo.spec.ts` in the projects `demo` and `demo-phone`: two browsers in two sandboxes without cross-talk over the API and the socket, logout, a lost sandbox replaced by itself, the full page with a mocked `503`, the per-address answer). The real cap is covered by the Identity tests; the e2e suite runs from one address and lifts both limits (`tests/e2e-web/demo.env`).

## Alternatives considered

- **Cloning a template account** (copy rows in every service with new ids): fast, but every service would need a clone endpoint that knows all its tables and id references, and Telemetry's history is the bulk of the rows anyway. The pool gets the same click time with the normal write paths.
- **History in the background after the claim**: the visitor's socket wakes the mowers within a second, and Telemetry drops history older than the first live state.
- **An own orchestration service**: a new image, pipeline and chart entry for a demo-only job; Identity already owns accounts and sessions and runs background jobs.
- **Deleting the account through events** (`identity.account.deleted` consumed by every service): durable, but every service would need a consumer and NATS permissions for an event only a demo uses, and Identity could not tell when the cleanup is complete. The explicit purge calls report completion and are retried.
- **A shared read-only view when full**: see "Limits".
- **Short grace period after the last socket closes** (delete minutes after the visitor leaves): reloads, tab switches and phones in the background would lose the sandbox; the idle time on session refreshes is robust, and resting mowers make an idle sandbox nearly free.
