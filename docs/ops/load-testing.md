# Load testing

How many people the stack serves at once, and what gives out first. The question that matters
most is the [public demo](demo.md) ([ADR 0068](../adr/0068-public-demo-mode.md)): one Compose
stack with three simulated mowers on a VPS with **2 vCPU and 7.8 GB**, where every visitor is a
login session of the same demo user and holds a live view.

The load test lives in [`tests/load`](https://github.com/lukislp/mowdeck/tree/main/tests/load):
[k6](https://grafana.com/docs/k6/) scenarios, run from the official `grafana/k6` image (nothing
to install but Docker and Node), and a runner that starts its own throwaway stack shaped like the
demo's host.

**Only against the stack it starts.** Never point the scenarios at the public demo, a home
installation or any other real server. The runner talks only to the stack it starts itself, over
that stack's own Docker network; it has no option for another address.

## What it measures

| Scenario | What a virtual user does |
| --- | --- |
| `demo-visitor` | Loads the web app with an empty cache (`index.html`, the preloaded files, `config.json`), "Try the demo", the app shell's calls, the mower list, one mower page (map, state, track of today and of 7 days, schedules, weather, zones, commands, permissions), then holds the live view (`/api/v1/realtime/ws`, [ADR 0018](../adr/0018-realtime-websocket-protocol.md)) for about 2 minutes: `auth`, `subscribe` to every mower, an application ping every 25 s and the inbox poll every 60 s, counting the `state` messages. Then the next visitor on the same virtual user. |
| `rest-mix` | A signed-in user clicking through the app: mower list, mower page, health page, schedules, notifications, reports, 5 s apart, with the web app's token refresh. |
| `command-burst` | Start, pause, resume and dock against the simulated mowers at a rising rate (1, 5, 10, 20 commands a second), each followed to its final status (`GET /fleet/commands/{id}`, as the web app does when the socket is quiet). |
| `rate-limits` | The per-address protection on its own: 30 "Try the demo", 900 requests and 25 live views from one address. |

The requests are the web app's (`apps/web`), traced from its pages; a real browser also loads a
few more lazy chunks and repeats some polls, so treat the numbers as a slightly optimistic floor.

**Thresholds** (the scenario fails when one is crossed): API p95 below 500 ms, static files p95
below 300 ms, fewer than 1 % failed requests, more than 99 % of the checks, and for the live view
more than 99 % of the sockets ready (`ws_connect_success`) and receiving states
(`ws_sessions_with_state`), p95 of "ready" below 1 s; commands p95 below 500 ms to accept and 5 s
to their final status.

### The stack under test

`node tests/load/load.mjs up` starts `infra/compose/docker-compose.yml` +
`docker-compose.demo.yml` + `tests/load/docker-compose.load.yml` as the Compose project
`mowdeck-load` (its own network, volumes and image tag; the gateway at `127.0.0.1:8590`) with
`infra/compose/.env.example` and `tests/load/load.env`:

- the demo exactly as on the host: demo mode in every service, the seed job (three mowers in
  three gardens, 7 days of history), the simulation at 3x, **the demo's rate limits unchanged**
  (1200 requests a minute and burst 600 per address, "Try the demo" 10 a minute and burst 20,
  20 live views per address, 500 in all);
- **every container pinned to the same two host cores** (`cpuset`, `LOAD_STACK_CPUSET`, default
  `0,1`), so the whole stack shares two cores like the VPS; k6 runs pinned to other cores
  (`LOAD_K6_CPUSET`, default `2-5`) and never competes with the stack;
- **memory limits** per service, about 7 GB together (the stack used under 2 GB in every run);
- the demo host's proxy setup: k6 is the trusted proxy (`GATEWAY_TRUSTED_PROXIES` = the stack's
  fixed subnet, `GATEWAY_CLIENT_IP_DEPTH=1`) and names each virtual visitor with its own address
  in `X-Forwarded-For`, exactly like Caddy in front of the demo. So every per-address limit counts
  each visitor, as in production, and nothing had to be loosened. What one address runs into is
  the `rate-limits` scenario.

## Run it

Needs Docker (with at least 6 cores, so that k6 has its own) and Node 22 or newer.

```bash
node tests/load/load.mjs up                     # build and start mowdeck-load, wait until healthy
node tests/load/load.mjs k6 rate-limits         # one scenario
LOAD_TARGETS=25,50,100 node tests/load/load.mjs k6 demo-visitor
node tests/load/load.mjs sweep                  # demo visitors level by level, stops when it breaks
node tests/load/load.mjs stats                  # one docker stats snapshot of the stack
node tests/load/load.mjs down                   # stop it and delete its volumes
node tests/load/load.mjs run                    # up, all four scenarios, down (LOAD_KEEP=1 keeps it)
```

Every `k6` run writes `tests/load/results/<time>-<scenario>/`: `summary.json` (k6's end of test
summary), `k6.txt` (its text output), `stats.jsonl` (a sample every 10 s: `docker stats` of every
container, Postgres connections, NATS `/varz`, Realtime's metrics) and `stats.md` (peak and average
per service). A sweep adds `sweep.md`, one row per level.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOWDECK_LOAD_PROJECT`, `MOWDECK_LOAD_PORT` | `mowdeck-load`, `8590` | Compose project and host port (never `mowdeck` or `mowdeck-demo`) |
| `LOAD_STACK_CPUSET`, `LOAD_K6_CPUSET` | `0,1`, `2-5` | cores of the stack and of k6 |
| `LOAD_TARGETS`, `LOAD_STEP` | `10,25,50`, `2m` | `demo-visitor`: visitors per ramp stage and how long each holds |
| `LOAD_LEVELS` | `25,50,100,200,300,400,500` | `sweep`: one `demo-visitor` run per level (60 s ramp, 4 min hold) |
| `LOAD_HOLD` | `120` | seconds a visitor keeps the live view open |
| `LOAD_USERS`, `LOAD_THINK` | `10,25,50`, `5` | `rest-mix`: users per stage, seconds between pages |
| `LOAD_RATES`, `LOAD_SENDERS` | `1,5,10,20`, `20` | `command-burst`: commands a second per stage, sending addresses |
| `MOWDECK_LOAD_COMPOSE_FILE` | | one more Compose file on top, to compare a tuning |
| `MOWDECK_LOAD_ENV_FILE` | | one more env file (CI: the image mirror) |
| `LOAD_DEBUG` | | log every answer of 400 or above |

**In CI** the workflow `load-test` (`.github/workflows/load-test.yml`) runs it by hand only
(`gh workflow run load-test.yml`, inputs for the scenarios, ramp targets and sweep levels), never
on pull requests. The runner has four cores: two for the stack, two for k6. The results are the
artifact `load-test-results`, the tables also go to the job summary. The CI job `compose` only
checks that the stack configuration renders, that the report script's tests pass and that every
scenario loads.

## Results

Measured on 2026-11-01 against `main` at f9b3d06 (before) and with the two fixes of this change
(after). The host is a Windows 11 workstation with Docker Desktop (32 logical cores, the stack
pinned to two of them, k6 to four others). A VPS core is usually slower than a desktop core, so
expect the demo host to reach each limit somewhat earlier. Levels of the sweep: all visitors
arrive within 60 s, then stay 4 minutes, each one visiting again after its live view ends (about
every 2.5 minutes).

### Demo visitors before the fixes

| Concurrent visitors | API p95 | Failed requests | Live views with states | Stack CPU avg / peak | OpenFGA CPU avg / peak |
| ---: | ---: | ---: | ---: | ---: | ---: |
| idle | | | | 68 % / 114 % | |
| 25 | 775 ms | 1.0 % | 92 % | 98 % / 204 % | 13 % / 90 % |
| 50 | 395 ms | 0 % | 100 % | 108 % / 235 % | 14 % / 84 % |
| 100 | **2049 ms** | **7.1 %** | **35 %** | 138 % / 208 % | 47 % / 161 % |

CPU in percent of one core: the stack has 200 %. **The first thing to give out was authorization,
at about 100 visitors.** Every request checks `can_view` on the mower in OpenFGA (cached for 5 s
per service). When a page loads, a dozen requests check the same mower at the same moment, and
in the demo every visitor is the same subject, so with a cold cache each of them asked OpenFGA on
its own. One check with this model costs about 20 datastore reads; 20 to 30 identical checks at
once already queued past the client's 2 s timeout on two cores. Timed-out checks are denied
(fail closed), which visitors saw as "mower not found" (404) and as a live view without states
(the subscription denied). The idle stack also used a third of the host: twelve Python
healthchecks every 5 s, each starting an interpreter (about 0.3 s of CPU) and pinging the
database and OpenFGA.

### Demo visitors after the fixes

| Concurrent visitors | API p95 | Static p95 | Failed requests | Live views ready / with states | Requests per s | Stack CPU avg / peak | Busiest service (avg / peak) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| idle | | | | | | 19 % / 94 % | |
| 50 | 140 ms | 12 ms | 0.01 % | 100 % / 100 % | 25 | 50 % / 130 % | OpenFGA (6 % / 51 %) |
| 100 | 140 ms | 12 ms | 0 % | 100 % / 100 % | 49 | 54 % / 136 % | Telemetry (11 % / 55 %) |
| 150 | 187 ms | 16 ms | 0 % | 100 % / 100 % | 72 | 63 % / 148 % | Telemetry (13 % / 43 %) |
| **200** | **298 ms** | 23 ms | 0 % | 100 % / 100 % | 95 | 83 % / 196 % | Telemetry (18 % / 69 %) |
| 300 | 1277 ms | 29 ms | 0 % | 100 % / 100 % | 143 | 102 % / 206 % | Telemetry (24 % / 83 %) |
| 400 | 6050 ms | 37 ms | 0.02 % | 100 % / 100 % | 171 | 119 % / 213 % | Telemetry (30 % / 85 %) |
| 300, arriving over 3 min | **209 ms** | 16 ms | 0 % | 100 % / 100 % | 123 | 89 % / 189 % | Telemetry (22 % / 61 %) |

- **About 200 concurrent demo visitors stay within the thresholds** (API p95 below 500 ms, no
  errors, every live view working) even when they all arrive within one minute. 300 visitors are
  fine when they arrive over three minutes (one new visitor every 0.6 s); arriving within one
  minute they push p95 above a second, 400 within one minute above 6 s. Nothing failed outright
  up to 400: requests got slow, not refused, and every live view stayed up.
- **The limit is the two cores**, and arrivals cost far more than staying: a first visit is
  about 45 requests within a few seconds, an open live view costs almost nothing.
- **Next bottleneck: Telemetry.** It serves most of a mower page (state, map, zones,
  capabilities, positions of today and of 7 days, tracking, aerial) in one Python process. At 300
  visitors its requests had a p95 of 250 to 700 ms while every other service stayed below 100 ms
  (per request: `endpoints.md` of the run).
- **Not a bottleneck:** the WebSocket fan-out (Realtime under 5 % CPU on average with 400 sockets
  and 100 state messages a second, nothing dropped for slow consumers, ready within 45 ms p95,
  first state within 70 ms p95), NATS (under 5 % on average, no slow consumers), OpenFGA after
  the fix (2 to 4 % average), memory (the whole stack under 1.9 GB of the 7 GB), the static
  files (p95 below 40 ms). Postgres had up to 77 client connections of `max_connections` 100
  (see the recommendations).
- The demo's limit of 500 live views in all (`DEMO_REALTIME_MAX_CONNECTIONS`) is above what the
  host serves with acceptable latency, so it is not the limit.

### Signed-in users clicking through the app

Each user opens a page every 5 s on average (mower list, mower page, health, schedules,
notifications, reports), far busier than a demo visitor who loads one mower page and then
watches. One level per run: 30 s ramp, 2 minutes hold (200: 4 minutes).

| Users clicking | Requests per s | API p95 | API p99 | Failed requests | Stack CPU avg / peak | Telemetry CPU avg | Postgres connections |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **25** | 30 | **239 ms** | 503 ms | 0 % | 59 % / 143 % | 20 % | 67 |
| 50 | 56 | 957 ms | 2.3 s | 0 % | 122 % / 214 % | 35 % | 78 |
| 100 | 93 | 3.0 s | 5.5 s | 0 % | 171 % / 201 % | 59 % | 91 |
| 150 | 94 | 9.4 s | | 0 % | 152 % / 208 % | | 80 |
| 200 | 114 | 12.2 s | | **9.1 %** | | | 90 |

- **About 25 users clicking through the app at the same time** stay within the thresholds;
  throughput tops out at about 95 requests a second (the two cores), beyond that requests only
  queue.
- At 200 users the requests did not just get slower: **Telemetry's healthcheck failed, Docker
  marked it unhealthy and Traefik stopped routing to it, so every Telemetry request got 404**
  for about 40 s at a time (fixed since, see [Overload without an outage](#overload-without-an-outage)).
- The slowest requests are all Telemetry's: at 50 users `zones` (p95 3.2 s, about six queries
  one after the other), `tracking`, `capabilities`, `battery`, `map`, `positions`,
  `health/daily`, `state`, then the Scheduler's schedule preview.

### Command burst

Start, pause, resume and dock against the three simulated mowers at 1, 5, 10 and 20 commands a
second (one minute each, 2 419 commands, from 20 addresses): every command accepted, p95 49 ms
to accept (`POST /fleet/mowers/{id}/commands`), p95 574 ms until its final status through Fleet,
NATS, the Device-Gateway and the simulator, no failed request. Fleet used 15 % CPU on average,
NATS 9 %. Commands are not a bottleneck; note that Fleet has no command limit of its own (see the
recommendations).

### Rate limits from one address

The per-address protection holds on the stack under test, with the demo's settings:

| From one address | Passed | Refused |
| --- | ---: | ---: |
| 30 "Try the demo" at once (`identity-ratelimit`, 10 a minute, burst 20) | 20 | 10 (429) |
| 900 requests at once (`demo-ratelimit`, 1200 a minute, burst 600) | 610 | 290 (429) |
| 25 live views (20 per address in the demo) | 20 | 5 (4429) |

So a load test from one address (or many visitors behind one NAT) gets 20 demo sessions, about
600 requests and 20 live views; the scenarios above therefore give each virtual visitor its own
address through the trusted proxy header, exactly as the demo's TLS proxy does, instead of
loosening any limit.

### Overload without an outage

Measured on 2026-11-01 as an A/B on the same host and in the same hour: `main` at 48c7e43 and
then the change of [ADR 0077](../adr/0077-liveness-readiness-and-routing.md) with the Telemetry
work below, each on a fresh stack (`LOAD_STACK_CPUSET=20,21`, `LOAD_K6_CPUSET=22-25`). Other
containers shared the workstation during both runs, so the absolute numbers are higher than in the
tables above; compare the two columns.

Signed-in users clicking (`rest-mix`, 200 users, 30 s ramp, 4 minutes hold), the level that used to
take Telemetry out of routing:

| | Before | After |
| --- | ---: | ---: |
| Failed requests | **8.7 %** (3 174, mostly 404 from the gateway) | **0 %** |
| Checks passed (`map 200`, `report 200`, ...) | 94.4 % | 100 % |
| Telemetry container unhealthy | 30 s (6 samples, routes gone) | never |
| Requests per s | 129 | 161 |
| API p95 / p99 | 9.7 s / 15.8 s | 5.7 s / 8.9 s |
| `zones` p95 | 21.5 s | 7.1 s |
| `state` p95 | 11.5 s | 7.9 s |
| Postgres connections, peak | 91 of 100 | 82 of 200 |

Two cores are still saturated at 200 clicking users (requests queue, the API p95 stays above the
threshold), but the stack degrades by getting slower, no longer by failing, and serves a quarter
more requests.

Demo visitors (`sweep` at 300, all arriving within 60 s):

| | Before | After |
| --- | ---: | ---: |
| API p95 | 3 320 ms | 1 252 ms |
| HTTP errors | 4.1 % | 0 % |
| Live views ready / with states | 100 % / **65 %** | 99.5 % / 99.5 % |
| Telemetry requests p95 (slowest) | 7.6 s (`zones`) | 3.0 s (`tracking`) |
| State messages per s | 64 | 97 |

At 60 clicking users (2 minutes, below saturation) the API p95 went from 279 to 146 ms, `zones`
from 773 to 309 ms, `tracking` from 502 to 253 ms, the 7-day positions from 323 to 282 ms, at the
same throughput (72 and 71 requests a second; the "before" run had a profiler attached to
Telemetry).

What changed:

- **Health model** ([ADR 0077](../adr/0077-liveness-readiness-and-routing.md)). The Compose
  healthcheck asks `/startupz`: it passes once every readiness check passed once (so `up --wait`
  and `deploy.sh` still wait for a ready service), then only asks whether the process answers.
  Load or a later dependency failure never makes a container unhealthy; Traefik additionally keeps
  the routes of an unhealthy container (503 instead of 404). Dependencies are still evaluated every
  30 s: log line on change and the metric `mowdeck_readiness_check_up`. The database check uses a
  connection of its own, never the request pool. Kubernetes: startup `/startupz`, liveness
  `/healthz`, readiness `/readyz` with thresholds that do not flap under load.
- **Telemetry: one pooled connection per request.** Profiling Telemetry under load (py-spy) showed
  the position tracks as the largest CPU cost (a dict and a validated model per row, two tracks of
  up to 5 000 points per mower page) and the zone view as the slowest request: six queries, each
  checking a connection out of the pool again (with a ping), so under load it waited in the pool's
  queue six times. Now the GET routes that read several times share one connection, checked out on
  the first read and returned when the route returns; the zone view reads the map snapshot and the
  latest state once; position tracks select only their columns and are built without per-row
  validation. The answers are unchanged.
- **Connection budget.** Pools are 5 + 5 per service (Telemetry 10 + 5) instead of 5 + 10, so
  connections are kept rather than opened and closed under load, and PostgreSQL runs with
  `max_connections=200`; the budget per service is in [Compose: Database
  connections](compose.md#database-connections) and checked in CI.
- **Logs of failed authorization checks name the cause.** Standard library records keep their
  `extra` fields in the JSON log (secrets masked), so `mowdeck_authz` shows
  `"operation": "check", "error": "TimeoutError"`.

Not changed: a second Uvicorn worker for Telemetry. With the stack's two cores already saturated a
second process would only compete with Postgres and the other services for the same CPU; the cost
per request was the lever. The 7-day RTK positions are still loaded with every mower page (the RTK
panel's shares need them); asking for them only when the panel is opened would be a web app
change.

## Fixed in the first load test change

- **One question in flight per check** (`libs/mowdeck-authz`, `OpenFgaAuthorizer`): a cacheable
  check joins an identical check another request of the process is already asking instead of
  asking OpenFGA again (metric `mowdeck_authz_coalesced_total`). Failures still deny, nothing
  failed is cached, a cancelled asker lets its waiters ask themselves, sensitive relations and a
  TTL of 0 never join. OpenFGA's CPU at 100 visitors went from 47 % to 2 % average, the failed
  requests from 7 % to 0.
- **Healthchecks of the Python services every 30 s once they are up** (`start_interval: 2s`
  while starting, so `up --wait` stays fast; Docker 25 or newer, and Docker 29 is required
  anyway): the idle stack went from 68 % to 19 % of one core.

## Recommendations

From the first run; 1, 4 and 7 are done, 2 in part:

1. **Overload turns into an outage through the healthcheck.** Done
   ([Overload without an outage](#overload-without-an-outage), ADR 0077): the healthcheck asks
   `/startupz`, readiness has its own database connection, Traefik keeps the routes of an
   unhealthy container.
2. **Telemetry is the next limit.** Partly done (one connection per request, the zone view's
   queries, lean position tracks). Still open: cache what changes rarely per mower (map, zones,
   capabilities, aerial availability) with ETags and `Cache-Control: private, max-age`, and let
   the web app ask for the 7-day RTK positions only when the RTK panel is opened, or in a compact
   form. Fewer, combined requests per page would help every service.
3. **Smooth arrivals at the edge.** Most of the cost is in the first seconds of a visit. A
   Cloudflare rule that caches `/_app/immutable/*` and a short queue or rate rule on "Try the
   demo" (already suggested in [Public demo](demo.md)) would keep a burst of new visitors (a
   link on a busy page) from slowing everybody down.
4. **Postgres connections.** Done: pools of 5 + 5 (Telemetry 10 + 5) instead of 5 + 10,
   `max_connections=200`, the budget in [Compose: Database connections](compose.md#database-connections).
5. **OpenFGA cost per check.** About 20 datastore reads per `can_view` check, and the
   `current_time` context at microsecond resolution means OpenFGA's own check cache cannot hit.
   Rounding `current_time` to the minute (support grants expire at most a minute late) and
   enabling `OPENFGA_CHECK_QUERY_CACHE_ENABLED` would cut OpenFGA's load further; it needs a
   decision on the expiry precision.
6. **Fleet has no command limit per user or session.** In the demo only the per-address gateway
   limit (1200 requests a minute) bounds how fast one visitor can command the shared mowers.
   A per-session limit of a few commands a second would not hurt anybody.
7. **Logs of failed authorization checks lose their cause.** Done: the JSON log keeps the
   `extra` fields of standard library records (secrets masked), so a denial names its
   `operation` and `error` (`TimeoutError`).
8. **Traefik's memory** grew with the open sockets and the per-address buckets to about 250 MB at
   400 visitors. If the host limits containers, give the gateway at least 512 MB.
9. **OpenFGA logs every request at INFO** (`grpc_req_complete` with the request body).
   `OPENFGA_LOG_LEVEL=warn` saves CPU and disk on a small host.
