# Playwright suite (web app gates)

The suite drives the web app in Chromium against a full Compose stack. It registers users, accounts and simulated mowers, links fake cloud accounts and grants an operator admin, so it **only runs against a throwaway test stack**, never against the stack you actually use. What the specs cover is in [docs/dev/web-app.md](../../docs/dev/web-app.md#testing).

## Run locally

```bash
pnpm install
pnpm --filter e2e-web exec playwright install chromium webkit firefox   # once
pnpm e2e:stack up        # build and start the test stack, wait until healthy (first build takes a while)
pnpm e2e:stack test      # projects phone and desktop; extra arguments go to `playwright test`
pnpm e2e:stack down      # stop it and delete its volumes
```

`pnpm e2e:stack run` does all three in one go (`MOWDECK_E2E_KEEP=1` keeps the stack afterwards). More commands: `screens` (review screenshots), `acceptance` (the Phase 1 pytest acceptance test `tests/e2e` against the same stack), `ps`, `logs [service]` and `compose <args>` (any `docker compose` command for the test stack). Single specs: `pnpm e2e:stack test tests/drive.spec.ts`, or `--project=desktop -g "fleet"`.

`pnpm --filter e2e-web test` and `pnpm --filter e2e-web screens` also work while the test stack is up: their default base URL is the test stack (`http://localhost:8480`).

## Browsers of the camera relay

`camera-relay-web.spec.ts` runs in Chromium, Firefox and WebKit. The relay sends H.264 only (ADR 0045).

- **Firefox on Linux** decodes H.264 only with Cisco's OpenH264 plugin, which a normal Firefox downloads by itself and Playwright's Firefox does not; without it the offer has no H.264, MediaMTX answers 400 ("codecs not supported by client") and the spec fails with that reason. CI fetches the plugin build that Firefox's update service names (pinned sha512, `.github/workflows/ci.yml`, step "OpenH264 for Firefox") and sets `MOZ_GMP_PATH`. Locally on Linux:

  ```bash
  dir=$HOME/.cache/mowdeck-gmp/gmp-gmpopenh264/2.6.0 && mkdir -p "$dir"
  curl -fsSL -o /tmp/openh264.zip https://ciscobinary.openh264.org/openh264-linux64-652bdb7719f30b52b08e506645a7322ff1b2cc6f.zip
  echo "f5246bf14d038adf4ce0c4360262ab722bc3de4220f047c3d542b4c564074b4877dc8659e3125c5171c749e7ce93f20cc63777eb5e1539e960670cbc5f30ac85  /tmp/openh264.zip" | sha512sum -c -
  unzip -q /tmp/openh264.zip -d "$dir" && export MOZ_GMP_PATH=$dir
  ```

  Firefox on Windows and macOS decodes H.264 through the system and needs nothing. The plugin is downloaded from Cisco at test time, never committed or redistributed.
- **WebKit** on Linux has WebRTC and H.264; Playwright's WebKit on Windows has no WebRTC, so the spec is skipped there with that reason.

## The test stack

`scripts/stack.mjs` runs `infra/compose/docker-compose.yml` plus the override `infra/compose/docker-compose.e2e.yml` as its own Compose project:

| What | Test stack | Why |
| --- | --- | --- |
| Project | `mowdeck-e2e` (`MOWDECK_E2E_PROJECT`) | own containers and volumes; `down -v` deletes only these |
| Network, Traefik scope | the project name | the stacks do not see or route each other's containers |
| Web and API | `http://localhost:8480` (`MOWDECK_E2E_PORT`), bound to 127.0.0.1 | does not collide with port 80 |
| Other host ports | port + 1 (HTTPS), + 2 (PostgreSQL), + 3 and + 4 (NATS), + 5 and + 6 (OpenFGA), all 127.0.0.1 | no collision |
| Images | `mowdeck/<service>:<project>` | a build never replaces the `:dev` images of your stack |
| Configuration | `infra/compose/.env.example` + `stack.env` (+ `MOWDECK_E2E_ENV_FILE`) | an `infra/compose/.env` with real accounts or keys is never read |

`stack.env` holds the suite's switches: static weather with the development endpoint and a 5 s scheduler tick, the fake vision provider and the fake Mammotion cloud (`*@fake.mowdeck.test`), so the stack talks to simulators only. To run two test stacks side by side (two worktrees), give each its own `MOWDECK_E2E_PROJECT` and `MOWDECK_E2E_PORT` (at least 7 ports apart).

## Safety guard

`docker-compose.e2e.yml` adds the service `e2e-marker`, which answers `GET /api/v1/e2e-stack` with the Compose project name as its host name. Before any test, `global-setup.ts` asks the base URL for it and **refuses to run** when it is missing, with a message that names the target and the command to start the test stack. A stack without the override (your own, at `http://localhost` or on the LAN) has no marker. The project name from the marker becomes `MOWDECK_E2E_COMPOSE_PROJECT`, which `grantPlatformAdmin` uses to find the identity container with `docker exec`, so the operator grant always lands in the stack under test. The pytest acceptance test (`tests/e2e`) checks the same marker.

To target another stack on purpose (for example a disposable remote one), set `MOWDECK_E2E_ALLOW_ANY_STACK=1` and `MOWDECK_E2E_BASE_URL`; without local Docker access add `MOWDECK_E2E_PLATFORM_ADMIN=off` (skips `operator.spec.ts`), otherwise set `MOWDECK_E2E_COMPOSE_PROJECT`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOWDECK_E2E_PROJECT` | `mowdeck-e2e` | Compose project of the test stack (`mowdeck` is refused) |
| `MOWDECK_E2E_PORT` | `8480` | host port of the test stack's web and API |
| `MOWDECK_E2E_BASE_URL` | `http://localhost:<port>` | where the suite points the browser |
| `MOWDECK_E2E_ALLOW_ANY_STACK` | unset | `1` skips the marker check |
| `MOWDECK_E2E_COMPOSE_PROJECT` | from the marker | project for `docker exec` helpers |
| `MOWDECK_E2E_PLATFORM_ADMIN` | on | `off` skips the specs that need an operator admin |
| `MOWDECK_E2E_RUN_ID` | random | suffix of every user and account name |
| `MOWDECK_E2E_ENV_FILE` | unset | extra env file for the test stack, e.g. image mirror lines |
| `MOWDECK_E2E_KEEP` | unset | `1`: `run` leaves the stack up |

## CI

The jobs `e2e-web` and `e2e` of `.github/workflows/ci.yml` start the stack on the runner with both Compose files (default project and port 80), so the guard passes on the marker there as well; they upload the report and the Compose logs when they fail.
