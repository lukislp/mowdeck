# Phase 1 acceptance test

`test_phase1_acceptance.py` proves the Phase 1 gate of [docs/phases.md](../../docs/phases.md) against the running Compose stack, through Traefik, with real HTTP and WebSocket clients (`httpx`, `websockets`). Helpers (configuration, login, sub-users, polling, the socket that keeps every message) are in `e2e_support.py`; there is no `conftest.py`.

The tests carry the marker `e2e` and are excluded from `uv run poe test` (`addopts = -m 'not e2e'`); the idle WebSocket check also carries `slow` (90 s).

## Scenario

| Step | Check |
| --- | --- |
| 1 | `lukas-<run>` registers and logs in |
| 2 | sub-user `anna-<run>` with the seeded Operator role on the account; she logs in |
| 3 | Lukas registers a simulated mower through Fleet |
| 4 | Telemetry reports `docked` within 30 s |
| 5 | Anna's socket: `auth`, `subscribe`, `subscribed` with the mower allowed |
| 6 | Anna's `start_mow` (area `front`) is 202; the socket shows `command_result` `executed` and a `state` `mowing` within 30 s; `GET /api/v1/fleet/commands/{id}` is `executed` |
| 7 | `return_to_dock` is 202; a `state` `returning` or `docked` within 60 s |
| 8 | `bob-<run>` (another account): 404 on the mower, on a command to it and on its telemetry, not listed, `subscribe` reports it in `denied` and no update reaches him; `vera-<run>` (Viewer in Lukas' account): `start_mow` 403, `emergency_stop` 202 and executed; requests without a token are 401, a socket with a bad token or without `auth` closes with 4401 |
| 9 | Anna's socket stays open for 90 s without traffic (the client sends no pings of its own) and still receives a state afterwards (`slow`) |

`<run>` is a random suffix per run, so the suite can run repeatedly against the same stack.

## Run locally

The test creates users, accounts and mowers, so it runs against the throwaway test stack (Compose project `mowdeck-e2e` at `http://localhost:8480`, see [tests/e2e-web/README.md](../e2e-web/README.md)), never against the stack you use. It refuses a stack without the e2e marker of `infra/compose/docker-compose.e2e.yml`.

```bash
uv sync
pnpm install
pnpm e2e:stack up
pnpm e2e:stack acceptance                         # all tests, about two minutes (uv run poe e2e)
pnpm e2e:stack acceptance -m "e2e and not slow"   # without the 90 s idle check
pnpm e2e:stack down
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOWDECK_E2E_BASE_URL` | `http://localhost:8480` | Traefik of the test stack (`MOWDECK_E2E_PORT`) |
| `MOWDECK_E2E_ALLOW_ANY_STACK` | unset | `1` runs against a stack without the e2e marker (on purpose only) |
| `MOWDECK_E2E_WS_URL` | derived | `ws://.../api/v1/realtime/ws` |
| `MOWDECK_E2E_HTTP_TIMEOUT` | `10` | per request and handshake |
| `MOWDECK_E2E_READY_TIMEOUT` | `30` | mower until `docked` |
| `MOWDECK_E2E_LIVE_TIMEOUT` | `30` | result and state on the socket |
| `MOWDECK_E2E_DOCK_TIMEOUT` | `60` | `returning` or `docked` |
| `MOWDECK_E2E_IDLE_SECONDS` | `90` | idle time through Traefik |

CI runs the same in the job `e2e` (`.github/workflows/ci.yml`) and uploads `docker compose logs` as the artifact `e2e-logs` when it fails.

## Troubleshooting

| Symptom | Likely cause and check |
| --- | --- |
| `stack not reachable` | The test stack is not up (`pnpm e2e:stack up`), or a service is not healthy: `pnpm e2e:stack ps` |
| `refusing to run against ...` | The base URL is not a test stack (no e2e marker): start one with `pnpm e2e:stack up`; never point the test at the stack you use |
| `register` answers 403 | Identity runs with `IDENTITY_REGISTRATION=closed`; the test needs `open` (the default in `.env.example`) |
| `register` or `session` answers 429 | Traefik's rate limit on the Identity auth routes (10 per minute, burst 20) after many runs in a row: wait a minute |
| Mower never `docked` | `docker compose logs device-gateway` (mower registered, loaded from fleet), `docker compose logs telemetry` (consumer running, no account mismatch warnings); Fleet answered 503 (OpenFGA) |
| `role 'operator'` missing | Identity's seeded roles changed; `GET /api/v1/identity/accounts/<id>/roles` |
| Anna's `start_mow` is 403 | The Operator assignment did not reach OpenFGA: `docker compose logs identity`; decisions are cached for 5 s |
| No `command_result` on the socket | The gateway rejected the command (`docker compose logs device-gateway`), or the socket subscribed after the result (results are not cached) |
| No `state mowing` but the result is `executed` | `docker compose logs realtime`: slow consumer closes (4430) or dropped states; the heartbeat (10 s) should still deliver the current state |
| Socket closes with 4401 | Token expired (10 minutes) or issuer and audience differ between Identity and `MOWDECK_AUTH_*` |
| Socket closes with 4429 | More than 10 sockets of one user or 50 of one account on the instance |
| Idle test fails at about 60 s | Traefik cut the upgraded connection: check `MOWDECK_REALTIME_PING_SECONDS` (protocol pings must be on); as a last resort set `--entrypoints.web.transport.respondingTimeouts.readTimeout=0` on Traefik and record it in the Realtime README |
| Flaky timing in CI | Raise `MOWDECK_E2E_LIVE_TIMEOUT` and `MOWDECK_E2E_READY_TIMEOUT`; never add sleeps |
| Leftover data | Usernames are unique per run; `docker compose down -v` resets everything |
