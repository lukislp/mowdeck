# assistant

Optional chat control add-on (ADR 0075): turns a typed request ("mäh morgen früh nur hinten") into a validated, confirmable proposal of mower actions. It executes nothing: the web app shows the proposal as a work order and, after the user confirmed, sends the actions through Fleet's command and the Scheduler's schedule endpoints with the user's token.

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; the layer rules and the definition of done are in `docs/dev/service-template.md`. Operations: `docs/ops/assistant.md`; for users: `docs/guides/chat-control.md`.

## Purpose

- **Optional.** Compose profile `assistant`, Helm `services.assistant.enabled` (off by default). No core service imports or calls it (`tools/repo-checks/tests/test_addon_isolation.py`); removing it changes nothing else.
- **Public API only, with the caller's token.** It verifies the bearer token (JWKS) and forwards it unchanged to Identity (`POST /me/permissions`), Fleet (`GET /mowers/{id}`), Telemetry (map, capabilities, state) and Vision (`GET /me/provider`, `POST /me/provider/structured`). No NATS user, no database, no secret, no OpenFGA access.
- **Owns no data.** Nothing is stored; the request text is never logged.

## Interfaces

All routes are mounted under `/api/v1` (OpenAPI at `/api/v1/openapi.json`). Traefik routes `/api/v1/assistant` to this service.

| Method | Path | Rights | Description |
| --- | --- | --- | --- |
| GET | `/api/v1/assistant/status` | any user | Ready? With the `provider` interpreter a configured AI provider is needed (`reason`: `no_provider`, `provider_unavailable`); the phrase parser is always ready |
| POST | `/api/v1/assistant/mowers/{mower_id}/proposals` | `can_view` on the mower | Interpret `text` (1 to 500 characters) in the user's `timezone`; answers the validated actions (`executable` only when all passed), notes, unavailable kinds and `expires_at` (5 min). Nothing is executed |
| GET | `/healthz`, `/readyz`, `/metrics` | none | Operational endpoints |
| GET | `/.well-known/mowdeck-module` | none | Module descriptor: no permissions, the UI area `/assistant` (`mower.control`) |

Problem types: 404 (mower not visible), 409 `assistant-provider-missing`, 422 (text, time zone), 429 `assistant-limit` (own limit or Vision's), 502 `assistant-provider-failed`, 503 (a service it reads did not answer).

## Events

None: the assistant neither consumes nor produces bus events.

## How a request becomes a proposal

1. **Catalog** (`domain/catalog.py`): the kinds the caller may use (`start_now`, `start_later`, `pause`, `resume`, `dock`, by relation as in ADR 0016), only for a mower that ever connected, starts only with map areas, immediate kinds by activity; the areas with their user-visible names.
2. **Interpreter**: `provider` asks the user's AI provider through Vision with a strict JSON schema generated from the catalog (`domain/schema.py`: kinds and area ids as `enum`, no free text) and the request as one JSON document the system prompt declares data; `phrases` (`domain/phrases.py`) parses a few German and English phrases without AI. With nothing offered the model is not asked.
3. **Validation** (`domain/validate.py`): shape, kinds, areas, times (2 minutes to 7 days ahead, in the user's time zone), windows (15 to 720 minutes), at most 3 actions with one immediate; all or nothing.

## Settings

All settings of `mowdeck-service-kit` apply (`MOWDECK_*`, any value also from `MOWDECK_<NAME>_FILE`). Service specific settings and changed defaults:

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_SERVICE_NAME` | `assistant` | Service name in logs, metrics and traces |
| `MOWDECK_HTTP_PORT` | `8000` | HTTP port |
| `MOWDECK_NATS_ENABLED` | `false` | The assistant has no bus |
| `MOWDECK_AUTH_JWKS_URL` | none | Identity's JWKS; without it every request is 401 |
| `MOWDECK_ASSISTANT_INTERPRETER` | `provider` | `provider` (the user's AI provider through Vision) or `phrases`; demo mode always uses `phrases` |
| `MOWDECK_ASSISTANT_IDENTITY_URL` | `http://identity:8000` | Identity |
| `MOWDECK_ASSISTANT_FLEET_URL` | `http://fleet:8000` | Fleet |
| `MOWDECK_ASSISTANT_TELEMETRY_URL` | `http://telemetry:8000` | Telemetry |
| `MOWDECK_ASSISTANT_VISION_URL` | `http://vision:8000` | Vision |
| `MOWDECK_ASSISTANT_UPSTREAM_TIMEOUT_SECONDS` | `5` | Timeout of one read |
| `MOWDECK_ASSISTANT_AI_TIMEOUT_SECONDS` | `60` | Timeout of one structured answer |
| `MOWDECK_ASSISTANT_REQUESTS_PER_MINUTE` | `20` | Proposals per user and minute (per replica) |

## Layers

| Package | Content | Rule |
| --- | --- | --- |
| `domain/` | action kinds, catalog, schema and prompt, validator, phrase parser | pure Python, no I/O |
| `application/` | use cases (`proposals.py`) and ports (`Platform`, `Interpreter`) | depends on `domain` and ports only |
| `adapters/api/` | FastAPI routers, schemas, error mapping | translate HTTP to use cases |
| `adapters/upstream.py` | the public API over HTTP with the caller's token | |
| `adapters/interpreters.py` | the provider interpreter (Vision) and the phrase parser | |
| `main.py` | wiring (`build_app`) and `main()` | the only place that knows every layer |

## Local start

```bash
uv sync
MOWDECK_ENVIRONMENT=dev MOWDECK_ASSISTANT_INTERPRETER=phrases uv run python -m assistant.main
curl http://127.0.0.1:8000/readyz
```

With the stack: `COMPOSE_PROFILES=assistant` and `IDENTITY_EXTRA_MODULE_URLS=http://assistant:8000` in `infra/compose/.env` (`docs/ops/assistant.md`). The Compose block is in `compose.snippet.yml` (already part of `infra/compose/docker-compose.yml`); Kubernetes: `infra/helm/mowdeck/templates/assistant.yaml`.

Container image (build context is the repository root):

```bash
docker build -f services/assistant/Dockerfile -t mowdeck/assistant:dev .
```

## Tests

```bash
uv run pytest services/assistant --cov=assistant
```

The tests use a fake AI provider with scripted (also hostile) answers and a mocked public API (`httpx.MockTransport`); none calls a real AI API.
