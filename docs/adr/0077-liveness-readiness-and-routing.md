# ADR 0077: Liveness, readiness and routing

## Status

Accepted (2026-11-01). Follow-up of the load test ([Load testing](../ops/load-testing.md), recommendation 1).

## Context

Every MowDeck service built on the service kit had two health endpoints: `/healthz` (the process runs) and `/readyz` (every readiness check passes: database, OpenFGA, NATS, the service's subscriptions, ...). The Compose healthcheck of every Python service asked `/readyz`, and so did, indirectly, everything that hangs on Docker health:

- `docker compose up --wait`, `deploy.sh` and `depends_on: condition: service_healthy` wait until a service is healthy;
- Traefik's Docker provider routes only to healthy containers: an unhealthy container loses its routes, and a request for them gets **404** from the gateway.

The load test showed what that does under overload. With about 200 users clicking through the app on two cores, Telemetry was saturated: its readiness checks waited behind the requests for a pooled database connection and for OpenFGA and ran into their 2 s timeout. After four failed checks Docker marked the container unhealthy, Traefik dropped its routes, and every Telemetry request got 404 for about 40 s at a time (9.4 % failed requests in the run; the web app showed "not found"). The service was slow, not broken; with one replica, taking it out of routing only turned slowness into an outage, and a 404 even tells clients the wrong thing.

The same coupling existed in every Python service, in the service template and, with different consequences, in the Helm chart: Kubernetes probes `/readyz` for readiness (a failing pod leaves its Service) and `/healthz` for liveness and startup.

### Options

1. **Keep the healthcheck on `/readyz`, with longer timeouts and more retries.** Lowers the risk, does not remove it: a long enough overload, or a short outage of a dependency, still takes the only replica out of routing.
2. **Healthcheck on `/healthz` (liveness only).** Load and dependencies never affect routing. But Compose would call a service healthy as soon as its process serves, before its first successful database or OpenFGA round trip: `up --wait` and `depends_on` would lose what they wait for.
3. **Route without Docker health** (a Traefik file provider, or Traefik's own HTTP health check of the backends). Possible, but it splits the configuration (the labels stay the routing source for Compose) and Traefik's health check would have the same problem with a saturated service unless tuned per service.
4. **A third endpoint that means "has started"**: it runs the readiness checks until they pass once, and from then on answers like liveness. Healthcheck and Kubernetes startup probe use it; readiness stays what it was, for the readiness probe, operators and metrics.

## Decision

**Option 4**, plus the changes that make readiness itself robust.

The kit serves three endpoints with three meanings:

| Endpoint | Meaning | Used by |
| --- | --- | --- |
| `/healthz` | liveness: the process serves requests; never touches a dependency | Kubernetes liveness probe |
| `/startupz` | started: 503 until every readiness check passed once (`app.state.started`), then 200 like `/healthz` for the rest of the process's life | Compose healthcheck, the images' `HEALTHCHECK`, Kubernetes startup probe |
| `/readyz` | readiness now: every check, 503 with the failing ones and while draining | Kubernetes readiness probe, operators |

- **Compose**: every Python service's healthcheck asks `/startupz` (every 2 s while starting, every 30 s after; timeout 10 s, the request's own 8 s, 3 retries). `up --wait`, `deploy.sh` and `depends_on` keep waiting for a ready service; afterwards only a hung process makes a container unhealthy. Traefik runs with `--providers.docker.allowEmptyServices=true`, so even then the routes stay and answer **503** (retry later) instead of 404.
- **Dependency trouble is still visible**, just no longer through routing: each service evaluates its checks every `MOWDECK_READINESS_INTERVAL_SECONDS` (default 30, 0 turns it off), logs `service not ready` with the failing checks and `service ready again` on recovery, and exports `mowdeck_readiness_check_up{check}` and `mowdeck_service_started`. A service whose database or OpenFGA is gone answers its requests with errors (503, or deny for authorization) by itself, which is what a client should see.
- **Readiness does not measure load**: the database check opens a connection of its own for each check (`NullPool`) instead of waiting for one of the request pool, so a pool busy with requests is not a broken database.
- **Kubernetes**: startup probe `/startupz` (every 2 s, up to 3 minutes), liveness `/healthz` (timeout 5 s, 6 misses = one minute, so a busy pod is not restarted), readiness `/readyz` every 10 s, timeout 5 s (above the checks' 2 s), 3 failures before a pod leaves its Service. Readiness still removes a pod whose dependencies fail, which is right when other replicas can take over, but a short slow phase does not flap it. The web app's nginx has `/healthz` only and keeps it for all three.

Alongside, as part of the same follow-up: the database pools are bounded and budgeted (`MOWDECK_DATABASE_POOL_SIZE`, `MOWDECK_DATABASE_MAX_OVERFLOW`, default 5 + 5, Telemetry 10 + 5; PostgreSQL `max_connections` 200 in Compose and the chart, checked by `tools/repo-checks`), and standard library log records keep their `extra` fields in the JSON log, so a denied authorization check names its cause.

## Consequences

- A saturated service stays routed. The reproduction (200 users clicking, two cores) no longer marks Telemetry unhealthy and has no 404 phases (numbers in [Load testing](../ops/load-testing.md)).
- `up --wait` behaves as before: a service is healthy only after its dependencies answered once.
- A dependency that fails after the start no longer turns a container unhealthy in Compose; whoever operates the stack watches the logs or the metric (or `/readyz`). With one replica per service that is the better failure mode; with several replicas behind Kubernetes the readiness probe still steers traffic away from a pod whose dependencies fail.
- A hung process is detected later than a failed readiness check used to be (up to about 90 s in Compose, one minute in Kubernetes). Docker does not restart unhealthy containers; Kubernetes does through liveness.
- New services get the model from the kit and the template; `tools/repo-checks` fails when a Compose healthcheck, a snippet, a Dockerfile or the chart's probes ask the wrong endpoint.
