# planner

Mowing patterns as plug-ins: computes, checks and previews spot mowing routes, finds the gaps a run left from the position track, and keeps pattern templates per account. A thin HTTP layer around the library [`mowdeck-patterns`](../../libs/mowdeck-patterns/README.md): every installed `mowdeck.patterns` entry point is a pattern, every plan passes the library's validation chain before the Planner answers, and plans are never stored (the same request gives the same plan and hash).

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; layer rules in `docs/dev/service-template.md`. Decision: [ADR 0030](../../docs/adr/0030-mowing-patterns-and-planner.md) (patterns as plug-ins, the validation chain, inline spot plans, execution by capabilities, the track query, templates as the one piece of own data).

## Purpose

- **Patterns**: `GET /patterns` lists key, name, description, the settings JSON Schema (the app builds its form from it) and the native hint (`stripes`, `diagonal`, `checkerboard`, `edge_only`).
- **Preview**: area plus pattern chain gives a validated plan with duration, mowable and covered area, coverage ratio, lengths, warnings, the segments as a GeoJSON-like collection (local metres, or `[lon, lat]` with `wgs84: true`) and how the mower can run it.
- **Plans to start**: `POST /plans` returns the same plus the `SpotMowPlan` (encoded segments, SHA-256 `plan_hash`) and the ready `StartMow` command; the client sends it to Fleet (`POST /api/v1/fleet/mowers/{id}/commands`), which gates `spot_mowing`, limits the size, records the hash and publishes it to the Device-Gateway.
- **Coverage and gaps**: `POST /coverage/gaps` asks Telemetry for the position track of a time range (`tracks.positions.query`), widens it to the cutting width and returns the covered share and the gap polygons (local and WGS84) with a ready `remow` chain step.
- **Templates**: saved chains per account ("2 border laps + diagonal + re-mow"), usable in a plan request as `template`.

### Where to mow

`area` is a map area (`area_id`), a spot polygon in local metres (`polygon`) or WGS84 (`polygon_wgs84`, `[lat, lon]`, converted with the map's geo reference), or a spot inside an area (`area_id` plus a polygon), each with optional `holes`. The map's no-go zones and obstacles are always left out. A drawn spot must lie inside the mower's mapped areas (5 cm drawing slack): the mower knows nothing beyond them. The map and capabilities come from the mower's snapshots on the `DEVICE` stream (`mower_id`), or a raw `map` is sent with the request (no mower: execution `unknown`).

### Execution modes

| `execution.mode` | Meaning |
| --- | --- |
| `custom_paths` | the mower follows routes (`capabilities.custom_paths`, e.g. the simulator): it drives the segments |
| `native` | the mower cannot follow routes, but the chain is one native pattern (or `border_fill` with a native fill, at most 4 laps) on a whole map area it supports: it runs `native_job` as its own job; the `SpotMowPlan` carries it as `native` |
| `unsupported` | neither: the start command would be rejected |
| `unknown` | a raw map without capabilities |

## Interfaces

All routes are mounted under `/api/v1` (OpenAPI at `/api/v1/openapi.json`); Traefik routes `/api/v1/planner` here. The account is always the verified token's `acc`. A mower of another account or one the caller cannot see is 404; visible without the relation 403.

| Method | Path | Required | Notes |
| --- | --- | --- | --- |
| GET | `/api/v1/planner/patterns` | a principal | not gated by the plan |
| POST | `/api/v1/planner/plans/preview` | `spot_mowing`; `can_view` on the mower | body `{mower_id or map, area, chain or template, mower?, wgs84?}` |
| POST | `/api/v1/planner/plans` | `spot_mowing`; `can_control` on the mower | adds `spot_plan` and `command` |
| POST | `/api/v1/planner/coverage/gaps` | `spot_mowing`; `can_view` on the mower | body `{mower_id, start, end, area?, min_gap_m2?, cutting_width_m?}`; without `area` the map area with the most track points |
| GET | `/api/v1/planner/templates` | a principal of the account | by name |
| GET | `/api/v1/planner/templates/{name}` | a principal of the account | 404 if missing |
| PUT | `/api/v1/planner/templates/{name}` | `spot_mowing`; `can_control` on the account | 201 created, 200 replaced; the chain is validated |
| DELETE | `/api/v1/planner/templates/{name}` | `spot_mowing`; `can_control` on the account | 204 |
| GET | `/healthz`, `/readyz`, `/metrics`, `/.well-known/mowdeck-module` | none | readiness covers the database, NATS and OpenFGA |

Errors: 422 `validation` for invalid settings (`errors` per setting), plans the validation chain refused (`errors` per finding), spots outside the map, too many vertices, too large areas or plans; 409 when the mower has sent no map yet or the template limit is reached; 503 when Telemetry does not answer the track query. Request bodies refuse unknown fields.

`mower` overrides the mower data per request: `cutting_width_m`, `overlap_m`, `turning_radius_m`, `max_speed_mps`, `transfer_speed_mps`, `safety_margin_m`. Speeds are capped by the mower's `max_speed_mps`.

### Module descriptor

Permission `planner.view` (an account key implied by `mower.view`; the API checks `can_view` and `can_control` on each mower); feature `spot_mowing` (granted to the plans `home` and `fleet` in `infra/entitlements/plans.yaml`, `unlimited` has `*`). With billing disabled every account has it.

## Events and subjects

| Direction | Subject | Kind | Payload | Notes |
| --- | --- | --- | --- | --- |
| consumed | `device.mower.>` | JetStream `DEVICE`, durable and queue `planner-device`, newest per subject first | `MowerMapSnapshot`, `MowerCapabilitiesSnapshot` | the snapshot directory (`mower_facts`); an older snapshot never replaces a newer one |
| request | `tracks.positions.query` | core NATS (Telemetry) | `PositionTrackRequest` / `PositionTrackReply` | the encoded position track for coverage |
| consumed | `event.identity.grants.changed` | JetStream, every instance | `GrantsChanged` | drops cached authorization decisions |
| consumed | `event.entitlements.plan.changed` | JetStream | `PlanChanged` | drops the cached plan snapshot (kit client) |

## Execution

Geometry is CPU-bound: each plan and each coverage query runs in a worker thread, at most `MOWDECK_PLANNER_MAX_PARALLEL` at a time per replica (more requests wait), so health checks stay responsive. Replicas are stateless apart from the database and scale on CPU.

## Data

PostgreSQL database `planner` (Alembic in `src/planner/adapters/db/migrations`, run by `planner-migrate`): `mower_facts` (newest map and capabilities per mower, from the bus) and `templates` (`account_id`, unique `name`, `chain` as JSON, `description`, timestamps). No plans are stored.

## Settings

Kit settings (`MOWDECK_ENVIRONMENT`, `MOWDECK_LOG_*`, `MOWDECK_NATS_URL`, `MOWDECK_DATABASE_URL[_FILE]`), OpenFGA (`MOWDECK_OPENFGA_*`), token verification (`MOWDECK_AUTH_JWKS_URL`), plans (`MOWDECK_ENTITLEMENTS_*`) and:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOWDECK_PLANNER_CUTTING_WIDTH_M` | `0.4` | cutting width (LUBA 2: 0.4 m) unless a request overrides it |
| `MOWDECK_PLANNER_OVERLAP_M` | `0.05` | overlap of neighbouring runs (spacing = width minus overlap) |
| `MOWDECK_PLANNER_TURNING_RADIUS_M` | `0` | turning radius checked by the validation chain (0: turns on the spot) |
| `MOWDECK_PLANNER_MOWING_SPEED_MPS` | `0.4` | mowing speed (capped by the mower) |
| `MOWDECK_PLANNER_TRANSFER_SPEED_MPS` | `0.6` | blades-off transfer speed (capped by the mower) |
| `MOWDECK_PLANNER_SAFETY_MARGIN_M` | `0.15` | distance of the mower's centre from every edge, hole and zone |
| `MOWDECK_PLANNER_MAX_VERTICES` | `2000` | vertices of a drawn spot (outline and holes) |
| `MOWDECK_PLANNER_MAX_AREA_M2` | `20000` | largest area of one plan |
| `MOWDECK_PLANNER_MAX_PLAN_BYTES` | `262144` | largest plan as JSON (Fleet's `MOWDECK_SPOT_PLAN_MAX_BYTES` must not be smaller) |
| `MOWDECK_PLANNER_MAX_PARALLEL` | `2` | plans computed at the same time per replica |
| `MOWDECK_PLANNER_MAX_TEMPLATES` | `100` | templates per account |
| `MOWDECK_PLANNER_TRACK_TIMEOUT_SECONDS` | `5` | timeout of the track query |
| `MOWDECK_PLANNER_TRACK_LIMIT` | `20000` | most track points per coverage query |
| `MOWDECK_PLANNER_COVERAGE_MAX_HOURS` | `48` | longest time range of a coverage query |

## Development

```bash
uv run pytest services/planner
MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres uv run pytest services/planner -k db
MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/planner -k nats
```

The tests run the real Fleet app next to the Planner on one in-memory bus (`tests/planner_support.py`): a plan from the Planner is started through Fleet and arrives at a fake Device-Gateway with its spot plan.

Example (Compose, a simulated mower):

```bash
curl -s -X POST localhost/api/v1/planner/plans/preview -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mower_id": "'$MOWER'", "area": {"area_id": "front"}, "chain": [{"pattern": "stripes"}]}'
```
