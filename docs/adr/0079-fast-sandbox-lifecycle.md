# ADR 0079: Fast demo sandbox claim and release

## Status

Accepted (2026-11-03). Amends [ADR 0076](0076-demo-sandboxes.md) (the pool, the claim and the sweep) and the demo history of [ADR 0068](0068-public-demo-mode.md); builds on the batched ingestion of [ADR 0078](0078-batched-state-ingestion.md).

## Context

A load test of the public demo host (2 vCPU, `tests/load/vps/ladder.sh`, scenario `sandbox-visitor`, all visitors arriving within 180 s) stayed within the thresholds up to 50 visitors, but the sandbox life cycle degraded first: at 30 visitors "Try the demo" (`POST /identity/demo/session`) had a p95 of 264 ms and the logout 260 ms, at 50 visitors 1.23 s and 3.1 s (average 2.6 s), while the host's CPU averaged only 57 %. Short bursts, not sustained load.

Profiling the claim and the release on the load test stack (`tests/load`, two pinned cores; py-spy on Identity, Fleet, Scheduler, Notifications and Telemetry; the new `--lifecycle` mode of `tools/repo-checks/scripts/demo_sandbox_cost.py`) showed:

- **The pool refill is the biggest CPU consumer of a burst.** Every claim asks for a refill, and a refill simulated a week for three mowers and ran about 6,700 states through Telemetry's ingestion: about 2.6 s of Telemetry CPU per sandbox on a desktop core (a third the simulation, the rest the write rules and the rows), about twice that on the VPS. The batching of ADR 0078 had already removed the per-state transactions; what was left is Python work per state. With 40 visitors in 60 s Telemetry ran at half a core for the whole minute, its event loop blocked for most of each batch, so every mower page and live view of the visitors already inside waited behind it. And still the refill kept up for only 16 of the 40 claims; the other 24 got sandboxes made on the spot without history.
- **Making a sandbox on the spot was serialised.** The account was created inside the claim lock, so a burst queued behind each other's account creation (OpenFGA writes), and then each made eight HTTP calls one after the other (three mowers, three schedules, the preferences read and written), each with a new HTTP client.
- **Unbounded fresh sandboxes overload the other services.** Without the lock, 40 visitors within 10 s asked Fleet, the Scheduler and OpenFGA for 38 accounts at once; OpenFGA checks queued past their timeout and were denied (fail closed), so the Scheduler answered "mower not found" for a mower Fleet had just made, and six claims failed.
- **The logout is cheap but not free.** `POST /identity/session/logout` only revokes the session (the purge runs later in the sweep); in Identity's single process 40 logouts at once still queue: 10 to 15 ms each on a desktop core while a pool refill ran beside them (the request handling, then a find and an update per session with a savepoint each, the audit row). On the VPS, in the scenario where every visitor leaves within the same second, this is the 2.6 s average.
- **The release took two sweep intervals.** A logged out sandbox was marked at the next sweep (up to 60 s), Fleet's purge stopped the mowers, and the other services were purged at the sweep after the 30 s settle time, so another 60 s later.
- **A fresh service refuses a burst of first tokens.** `mowdeck_auth.JwksCache.key_for` started one JWKS fetch and every other lookup during that fetch found neither a key nor the right to fetch (the cooldown), so it answered "unknown signing key" (401 or the socket's 4401). A freshly started service after a deploy or a reset meets exactly that with the first visitors' live views.

## Decision

### The demo history is copied from a daily template

Every sandbox of a day gets the same past days: the jobs start at the same hour each past day, in the same three gardens, and nothing of them depends on the mower but its ids. Telemetry (`telemetry.application.demo_history.DemoHistory`, behind the unchanged `POST /telemetry/demo/history`) therefore simulates a garden's days once per UTC day, for a **template mower** of a template account nobody can sign in to (fixed UUIDv5 ids from the garden options, the number of days and the date), through the normal batched ingestion. Every mower that asks for that garden on that day gets a copy of the template's rows with its own ids: `Tx.copy_ingested`, one `INSERT ... SELECT ... ON CONFLICT DO NOTHING` per table (history, positions with the thinning mark, the latest row whose JSON state is rewritten to name the mower and account, the health and day totals). A copy is exactly what the ingestion would have stored for the mower itself (tested on the memory store and on PostgreSQL).

- The template rows belong to an account without users, Fleet mowers or directory entry: no query of a visitor reaches them; retention and thinning treat them like any mower's.
- Making a day's template drops those of the seven days before. Which templates are complete is remembered in memory only; after a restart a template is made again (its leftovers dropped first), so an interrupted one is never copied.
- A mower that already has a state gets nothing, as the ingestion would refuse the older states; a mower of another account is refused (`AccountMismatchError`).
- The blade hours threshold event of the template's history is not repeated per copy (a week does not reach the 50 h default).

The shared demo's seed job uses the same endpoint and gets the same history.

### The pool is larger, refills and claims are bounded

- `DEMO_SANDBOX_POOL` defaults to **8** (was 2). A resting pool sandbox costs only its rows (ADR 0076) and a refill now takes about a second, so a burst of eight is served from the pool at once and the refill keeps up with about one visitor a second. It still refills one sandbox at a time.
- The claim decision stays serialised (the cap holds), but making a sandbox on the spot is not: the claim reserves a place in memory that counts to the cap until the sandbox's row exists, then makes it outside the lock. At most `MOWDECK_IDENTITY_DEMO_SANDBOX_MAKE_PARALLEL` (3) are made at once; a visitor waiting for a turn takes a sandbox the refill made meanwhile instead (it then has history too).
- The provisioner keeps one HTTP client with its connections and runs what does not depend on each other side by side: the first mower alone (it makes the account's default site in Fleet), the other two together, then the three schedules with the preferences read, then the preferences write: four round trips instead of eight. A failing call fails the provisioning only after the others ended.

### Release

- Revoking a login session family is one `UPDATE` (`Table.update_where`) instead of a find and an update with a savepoint per session; the sweep's revocation of a marked sandbox's sessions too.
- The sweep runs 5 s after a logout (so a burst of logouts is swept once) and, after Fleet's purge, when the settle time is over instead of at the next interval. A logged out sandbox is gone about 45 s later instead of up to two minutes. The logout itself still only ends the session: the purge in every service stays in the background, retried until complete.

### JWKS lookups wait for a fetch in flight

`JwksCache.key_for`, when a key id is unknown and a fetch is running (the lock is held), waits for that fetch and looks again instead of refusing the token. No extra fetch: random key ids during a fetch wait once and are refused afterwards, and the cooldown still limits fetches.

### Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEMO_SANDBOX_POOL` (`MOWDECK_IDENTITY_DEMO_SANDBOX_POOL`) | `8` (was `2`) | Ready sandboxes |
| `MOWDECK_IDENTITY_DEMO_SANDBOX_MAKE_PARALLEL` | `3` | Sandboxes made on the spot at once |
| `MOWDECK_IDENTITY_DEMO_SANDBOX_MAKE_ATTEMPTS` | `3` | Tries per visitor before `503 demo-full` (added 2026-11-08, see Consequences) |
| `MOWDECK_IDENTITY_DEMO_SANDBOX_MAKE_RETRY_SECONDS` | `2` | Pause between two tries |

`demo/demo.env` needs no new key. A `demo.env` that sets `DEMO_SANDBOX_POOL=2` explicitly keeps the old pool; remove the line (or raise it) to get the new default.

## Measurements

The load test stack of `tests/load` (two pinned cores of a desktop host, the demo's settings), `main` (f44400e) against this change, `demo_sandbox_cost.py --lifecycle`: N visitors click "Try the demo" within the ramp from their own addresses, each opens the mower list, one mower, its state and `/identity/me`, then all log out at the same moment. Times measured at the client after the connection was open. A VPS core is slower; expect the demo host to show the same shape at higher numbers.

| 40 visitors | within 60 s, before | within 60 s, after | within 10 s, before | within 10 s, after |
| --- | ---: | ---: | ---: | ---: |
| Claim median / p95 / max | 511 / 1092 / 2214 ms | 43 / 157 / 245 ms | 8940 / 13715 / 13760 ms | 3285 / 6918 / 7239 ms |
| Claims served by the pool (with history) | 16 of 40 | 40 of 40 | about 3 of 40 | 15 of 40 |
| Failed claims | 0 | 0 | 0 | 0 (6 without the bound on sandboxes made at once) |
| First view median / p95 | 337 / 517 ms | 362 / 431 ms | 615 / 1428 ms | 514 / 697 ms |
| Stack CPU while claiming (Telemetry), % of a core | 152 (49) | 105 (14) | 199 (42) | 198 (14) |
| Every sandbox deleted after the logouts | 120 s | 53 s | 107 s | 54 s |

The logout, measured inside the stack's network straight at Identity (40 sandboxes claimed, then 40 logouts at once): median / p95 / max **356 / 651 / 665 ms before, 211 / 367 / 381 ms after**. (Measured from the workstation through Docker Desktop's port forwarding the same burst varied between 240 and 650 ms in both versions.)

The scenario `sandbox-visitor` of the load test (`LOAD_VUS=40 LOAD_RAMP=180s LOAD_DURATION=2m LOAD_HOLD=60`: the visitors arrive within three minutes, each starts a mower and holds the live view, all log out at the end, k6 inside the stack's network):

| 40 sandbox visitors | Before | After |
| --- | ---: | ---: |
| API p95 | 1.30 s (threshold crossed) | 172 ms |
| "Try the demo" p95 / max | 6.09 s / 9.08 s | 98 / 160 ms |
| Logout p95 | 376 ms | 292 ms |
| `GET /telemetry/mowers/{id}/state` p95 | 2.0 s | 213 ms |
| First live state p95 | 662 ms | 107 ms |
| Failed requests | 0.16 % | 0 % |
| Stack CPU average (Telemetry) | 157 % (40 %) | 93 % (15 %) |

With the cap raised for the run (`DEMO_SANDBOX_MAX=60`), 60 such visitors: "Try the demo" p95 271 ms, logout p95 602 ms, API p95 582 ms (just above the 500 ms threshold), 0.08 % failed requests, 99.5 % of the live views with states, the stack at 133 % on average and at its 200 % in peaks: beyond 40 the limit is again the watched mowers' telemetry (ADR 0078), not the life cycle.

## Consequences

- Bursts of visitors are served from the pool with history; the pool refill no longer blocks Telemetry, so the visitors already inside keep fast mower pages.
- Telemetry keeps one template mower per garden per day (about 7,000 rows each); the first pool fill of a UTC day pays the simulation once.
- A sandbox made on the spot still has no history (a burst beyond pool and refill), and a visitor may wait a few seconds for a turn instead of failing.
- Addendum (2026-11-08): right after a start of the stack, the first authorization checks against a cold OpenFGA exceed the 2 s timeout, so Fleet answers `503` and the Scheduler denies (`404`), the first sandboxes of the pool and of the first visitors failed, and the CI job `e2e-demo` was flaky (the first "Try the demo" of a run). A sandbox made on the spot is now tried `MOWDECK_IDENTITY_DEMO_SANDBOX_MAKE_ATTEMPTS` (3) times with a fresh sandbox, `MOWDECK_IDENTITY_DEMO_SANDBOX_MAKE_RETRY_SECONDS` (2) apart, before the visitor is turned away; each failed sandbox is purged as before. The pool refill already retries on its next round.
- Tests: Telemetry (`test_telemetry_demo_history.py`: a copy equals the direct ingestion on memory and PostgreSQL, the thinning mark, idempotence, a new day's template, a restart, another account), Identity (`test_identity_demo_sandbox_bursts.py`: parallel creation under the cap, the bound and a waiting visitor taking a refill, a failed creation giving its place back, the sweep after a logout and after the settle time; the provisioner's parallel calls on one client; `update_where` on both stores), `mowdeck-auth` (a burst on a fresh cache, random key ids during a fetch).

## Alternatives considered

- **Delete asynchronously on logout**: it already was; the logout only ends the session. What remained was the per-request cost in a burst, cut by the one-statement revocation.
- **Cloning whole template accounts in every service** (ADR 0076's rejected alternative): Telemetry's history is where the cost was; mowers, schedules and preferences are a handful of rows through the normal APIs.
- **Caching the simulated states in memory and ingesting them per sandbox**: saves the simulation (a third) but not the ingestion, and holds thousands of state objects per garden.
- **Fewer history days during a burst**: cheaper, but the visitor of a burst would see less than the others, and the copy makes it unnecessary.
- **History after the claim, in the background**: Telemetry drops states older than the first live one (ADR 0076).
- **Several Identity worker processes**: the pool, the cap and the sweep hold state in one process; a second process would need them in the database first.
