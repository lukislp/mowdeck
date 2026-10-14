# ADR 0025: Home Assistant integration as a pure API client

## Status

Accepted (2026-10-09). Update (2026-10-11): the integration moved out of the monorepo into its own repository [`lukislp/mowdeck-hacs`](https://github.com/lukislp/mowdeck-hacs); the paragraphs below about the monorepo folder, `publish-hacs` and the path-filtered workflow describe the earlier setup. The decision itself (pure API client, no shared Python code, own uv project) stands.

## Context

Phase 3 ("Comfort") promises control from Home Assistant through an own HACS integration over the backend API (project plan, feature table and tech stack: "Custom Integration for HACS, own repo, config flow with URL + API token"). The account model already has the integration role: an API token with chosen permissions, exchanged at `POST /api/v1/identity/token` for a 10 minute access token ([ADR 0015](0015-tokens-sessions-and-key-rotation.md)). Live states and command results reach clients over the Realtime WebSocket ([ADR 0018](0018-realtime-websocket-protocol.md)); commands go through Fleet and are authorized per command kind ([ADR 0016](0016-fleet-commands-and-authorization-per-command-kind.md)). The community Mammotion integration (spike 0001, section 3.3) shows which entities and actions Home Assistant users expect.

Forces:

- HACS installs only `custom_components/<domain>/` of a repository that has `custom_components/` and `hacs.json` at its root, one integration per repository ([HACS: integration repositories](https://hacs.xyz/docs/publish/integration/)). The `hacs/action` validates a GitHub repository through the API ([HACS action](https://hacs.xyz/docs/publish/action/)), so a subfolder of the monorepo cannot pass it.
- Home Assistant pins its whole dependency tree (2026.10.0: aiohttp 3.14, `probatio` instead of voluptuous, Python 3.14.2 or newer), which conflicts with the services' locked versions and their Python 3.13 ([ADR 0010](0010-python-3-13.md)).
- Home Assistant's test harness (`pytest-homeassistant-custom-component`) needs a `conftest.py` and does not run on Windows (`fcntl`).
- Home Assistant may run outside the MowDeck host (another machine, behind Tailscale), and a revoked token must not leave a silent, stale integration.

## Decision

**A pure API client with no shared Python code.** The integration (`integrations/home-assistant/custom_components/mowdeck`) contains its own small aiohttp client (`api.py`) on Home Assistant's shared session and declares `requirements: []`. It imports nothing from `libs/` or `services/`; field names follow the committed OpenAPI documents (`apps/web/openapi/*.json`), and errors are read as problem+json documents (401 to reauth, 403 to a "permission" error, 404 to "not found", 5xx and network errors to "cannot connect", idempotent requests retried with exponential backoff, commands never resent after the request may have reached Fleet).

**Token exchange in the client.** The config flow asks for the base URL and an API token, exchanges it, reads `/identity/me` and lists the mowers; the entry's unique id is the account id. The client refreshes the access token 60 s before expiry and once more after a 401. A rejected exchange raises `ConfigEntryAuthFailed`, which starts Home Assistant's reauth flow; the new token must belong to the same account.

**Push over the WebSocket, polling as fallback.** One socket per entry sends `auth`, waits for `ready`, subscribes to every mower, and sends a fresh token with another `auth` before expiry. Pushed `state` messages update the coordinator's data and the entities at once without resetting the poll timer; `command_result` becomes the event `mowdeck_command_result`; `revoked` makes the mower unavailable. The coordinator polls every 60 s (option) while the socket is down and every 5 minutes while it is up, which also discovers new mowers and refreshes capabilities and maps (map with `If-None-Match`). The socket reconnects with jittered exponential backoff (1 to 60 s, 120 s after 4429); on 4401 it exchanges a new token, and when a fresh token is refused twice in a row a persistent Repairs issue explains it until a connection succeeds. Hence `iot_class: local_push`.

**Entities follow the Home Assistant platforms, not the Mammotion app.** One device per mower; `lawn_mower` (start resumes a paused job, otherwise starts the area of the Area select or all areas; pause; dock), sensors, binary sensors, buttons, numbers with ranges from the capabilities, an area `select` (one value) plus the action `mowdeck.start_mow` for several areas and pattern settings, and a GPS `device_tracker`. Entities that need features MowDeck does not expose yet (blade switch, rain sensor, firmware update, camera) are left out.

**A dedicated HACS repository published from the monorepo.** The source of truth stays `integrations/home-assistant` (one PR changes API and integration together). The manual workflow `publish-hacs.yml` mirrors the folder (with `LICENSE` and the folder's own `.github/workflows/validate.yml`) to `mowdeck-hacs` and optionally tags `v<manifest version>`. There, `validate.yml` runs `home-assistant/actions/hassfest` and `hacs/action`. In the monorepo the CI job `home-assistant` runs the integration's own uv project (lint, mypy, pytest with coverage, an offline HACS layout check) and the hassfest image on the folder, because the hassfest action only mounts the repository root.

**Its own uv project.** `integrations/home-assistant` is not a workspace member (the workspace globs do not include it); it pins `homeassistant==2026.10.0` and `pytest-homeassistant-custom-component==0.13.371` and uses Python 3.14. Only ruff runs from the root over it, with the root configuration.

Considered and rejected: depending on `mowdeck-contracts` (HACS would have to install it from PyPI, and its pydantic and Python pins would have to match every Home Assistant release); polling only (seconds of delay for start and stop, and needless load); an MQTT bridge with Home Assistant's MQTT discovery (a second protocol and broker to secure, and no per-token authorization); keeping the integration only in the monorepo (HACS cannot install from a subfolder).

## Consequences

- Home Assistant is an ordinary client: it sees exactly what its token may see, revoking the token in the app cuts it off within one request or one WebSocket re-check, and no backend change was needed.
- The API is now a public contract for an independently released client: breaking changes to `/api/v1` need a new version or a coordinated integration release. The integration's tests use a fake API built from the OpenAPI field names, so a renamed field shows up only in the end-to-end check or in production; follow-up: a contract test that loads `apps/web/openapi/*.json` and checks the fake against it.
- Two Python versions in one repository (3.13 for services, 3.14 for the integration) and a Linux-only test run for the integration (CI, WSL or Docker on Windows).
- Releases are manual: bump `version` in `manifest.json`, run `publish-hacs` with `release`. The dedicated repository needs the secret `HACS_PUBLISH_TOKEN` and, for the monorepo CI step, the variable `MOWDECK_HACS_REPOSITORY`.
- Follow-up: inclusion in the HACS default list (needs the dedicated repository with topics and a description), entities for schedules and rain pause when the Scheduler exists, and a `reconfigure` step to change the URL.
