# scheduler

Mowing schedules with rain pause that start mowers through Fleet. A schedule fires weekly on chosen days, every N days from an anchor date or once on a day, at a wall-clock time in its site's time zone, for one mower or every mower of a group. Before each start it re-checks that the schedule's owner may still control the mower, skips busy mowers, asks the weather, and then sends `start_mow` through Fleet on the owner's behalf, so Fleet keeps the single command record and audit.

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; layer rules in `docs/dev/service-template.md`. Decision: [ADR 0024](../../docs/adr/0024-scheduler-recurrence-rain-pause-and-command-issuing.md) (recurrence on `zoneinfo`, `fleet.commands.issue`, `SKIP LOCKED` ticker, weather port and location). The web app's schedules module (`apps/web`, [guide](../../docs/dev/web-app.md#schedules)) is its UI.

## Purpose

- **Schedules** per account, for a `mower` or a `group` target; kinds `weekly` (`days_of_week` and `start_time`), `every_n_days` (`anchor_date`, `interval_days`, `start_time`) and `once` (a single run on `anchor_date` at `start_time`, which must lie in the future when it is created; afterwards `next_run_at` stays null; used by chat control's later starts, ADR 0075). The time zone is the target's site (Fleet), or `timezone` to override it. DST: a time that does not exist fires one hour later on the same day (02:30 on the spring change fires at 03:30); a time that exists twice fires once, at the first occurrence, unless that one had already passed without a firing when the next run was computed (a schedule created or changed in between, typically during the repeated hour), then at the second. A day never fires twice (`last_run_at` guards it); see ADR 0024, addendum of 2026-10-25.
- **Window**: `duration_minutes` after the start time; a firing after the window closed (downtime) is recorded as `skipped_window`, the Scheduler never catches up; `return_to_dock_at_window_end` sends a still mowing job back to the dock.
- **Areas and patterns**: `area_ids` for a mower (empty: every area of the mower's map), all areas of each mower for a group; each start sends a `StartMow.pattern` shaped like the contracts' `MowPattern` (`rules.pattern_for_run`): stripes with one border lap (`name`, `angle_deg`, `border_laps`), overlaid by the mower's saved mowing defaults (`TargetMower.mow_pattern`, from Fleet's `PUT /mowers/{id}/mow-defaults`), then by the schedule's own `pattern` keys, then by the angle of this run; `rotation_angles` are used round robin, one per started run, to rotate the direction. Angles go out as `angle_deg` normalised to 0 up to 180 degrees. A key nobody sets is not sent (the mower keeps its own setting); a `start_progress_pct` of the defaults never reaches a scheduled run.
- **Start settings** ([ADR 0024 addendum 2026-10-24](../../docs/adr/0024-scheduler-recurrence-rain-pause-and-command-issuing.md#addendum-start-settings-per-schedule-2026-10-24)): `pattern` is a `SchedulePattern` (`name`, `angle_deg`, `border_laps`, `spacing_m`, `obstacle_detection`, `obstacle_laps`, `order`; each optional, no `start_progress_pct`), plus `blade_height_mm` and `speed_mps`, sent as `set_blade_height` and `set_speed` before each `start_mow`. Precedence: schedule over mow defaults over the mower. Creating a schedule, or changing its settings or target, checks them against the capabilities of every mower of the target (a group offers what all members offer; a mower with unknown capabilities only the angle and border laps): 422 `start settings do not fit <mower>: <reason>`. At fire time a setting the mower does not offer (any more) is left out and named in the run's details (`left out (not offered by the mower): ...`).
- **Angle rotation** ([ADR 0061](../../docs/adr/0061-mowing-angle-rotation.md)): per schedule off, `rotation_angles` (round robin, per schedule) or `rotation_step_deg` (1 to 179 degrees; the two exclude each other). With a step each run takes the last angle the mower mowed the run's areas with (the area mowed most recently wins) and turns it by the step, modulo 180; without history it uses the base angle (schedule, mow defaults, 0). Every started run stores the angle it sent per mower and area (`mow_angles`), so schedules and manual starts that opt in continue from each other; `edge_only` and `random_angle` never rotate. Runs (`RunOut`, `scheduler.run.recorded`) carry `angle_deg`, the preview the angle of each upcoming firing for the target's first mower (`angle_mower_id`). The Mammotion adapter sends the angle as `NavReqCoverPath.toward` with `toward_mode = absolute_angle`.
- **Rain pause** per schedule: `enabled`, `probability_threshold` (60 %), `precipitation_mm_threshold` (0.5 mm), `lookahead_hours` (3), `cooldown_hours` after rain (2), `run_when_unknown` (true). the lawn counts as wet until the end of the last hour at or above the millimetre threshold plus `cooldown_hours` (times 1.5, at most 6 h, when 5 mm or more fell in the last 6 h; the weather endpoint returns `wet`, `wet_until`, `recent_mm_6h`, `recent_mm_24h` and the past 12 hours with `past: true`), and a run does not start on a wet lawn. `pause_running_jobs_on_rain` (default true) sends a running scheduled job back to the dock when it starts to rain.
- **Authorization is never bypassed**: create and change need `can_edit_schedule` and `can_control` on the target; the caller becomes the owner (not for a change of `enabled` alone or a skip of the next run). Every firing checks the owner's `can_control` per mower again (`skipped_permission`); after `MOWDECK_SCHEDULER_MAX_PERMISSION_SKIPS` firings in a row skipped this way the schedule is disabled and `scheduler.schedule.disabled` is published. Fleet checks the same relation again for the subject before it records the command.
- **Runs**: one record per mower and attempt (`started`, `skipped_rain`, `skipped_permission`, `skipped_window`, `skipped_mower_busy`, `skipped_manual`, `failed`), each published as `scheduler.run.recorded`.
- **iOS widget overview** ([ADR 0059](../../docs/adr/0059-ios-widgets-and-live-activity.md)): `GET /mowers/{id}/widget` answers the next three runs of a mower (direct or through a group; not disabled or skipped), the rain pause's verdict for now (`runs`, `rain_pause`, `unsure`) and a few rain facts.

## Interfaces

All routes are mounted under `/api/v1` (OpenAPI at `/api/v1/openapi.json`); Traefik routes `/api/v1/scheduler` here. The account is always the verified token's `acc`. A schedule is guarded by its target: schedules of other accounts and schedules whose target the caller cannot see are 404; visible without the relation is 403. The same relation on the whole account also admits a schedule whose target was removed in Fleet (so an owner can still see and delete it).

| Method | Path | Required relation (on the target) | Notes |
| --- | --- | --- | --- |
| GET | `/api/v1/scheduler/schedules` | `can_view` per schedule | `can_view` on the account short-circuits, else `list_objects(can_view)` for mowers and groups |
| POST | `/api/v1/scheduler/schedules` | `can_edit_schedule` and `can_control` | 201 with `Location`; a foreign or unknown target is 404 |
| GET | `/api/v1/scheduler/schedules/{id}` | `can_view` | |
| PATCH | `/api/v1/scheduler/schedules/{id}` | `can_edit_schedule` and `can_control` (old and new target) | the caller becomes the owner, except when the body changes only `enabled` (switching on or off keeps the owner, so a toggle by an API token does not hand the schedule to it; re-enabling a schedule the Scheduler disabled itself, `disabled_reason` set, still makes the caller the owner); `next_run_at` is recomputed; `timezone: null` follows the site again |
| DELETE | `/api/v1/scheduler/schedules/{id}` | `can_edit_schedule` | 204, runs are deleted with it |
| POST | `/api/v1/scheduler/schedules/{id}/skip-next` | `can_edit_schedule` | the next firing is recorded as `skipped_manual`; 409 without a next run |
| POST | `/api/v1/scheduler/schedules/{id}/run-now` | `can_control` | 202 with the runs; on behalf of the caller; `?respect_rain=true` applies the rain pause; the next firing stays |
| GET | `/api/v1/scheduler/schedules/{id}/runs` | `can_view` | newest first, `?limit=&before=` (keyset on `started_at`, `next_before`) |
| GET | `/api/v1/scheduler/schedules/{id}/preview` | `can_view` | `?count=5` (at most 20): next firings, skipped flag, the mowing angle of each firing, the rain decision for the first one |
| GET | `/api/v1/scheduler/weather/{mower_id}` | `can_view` on the mower | the rain decision now, the forecast hours it used, `pause_until` (when a hold ends) and `settings` (the thresholds applied); thresholds as query parameters |
| GET | `/api/v1/scheduler/mowers/{mower_id}/angles` | `can_view` on the mower | the last angle per area and the step of manual starts (ADR 0061) |
| PUT | `/api/v1/scheduler/mowers/{mower_id}/angles/settings` | `can_control` on the mower | `{manual_step_deg: 1..179 or null}`: the step the start sheet offers |
| POST | `/api/v1/scheduler/mowers/{mower_id}/angles` | `can_control` on the mower | `{area_ids, angle_deg}`: a manual start with rotation chosen reports the angle it sent |
| PUT | `/api/v1/scheduler/dev/weather` | any principal | development only (`MOWDECK_SCHEDULER_DEV_ENDPOINTS`, static provider): `{mode: dry, rain, unknown}`; 404 otherwise |
| GET | `/healthz`, `/readyz`, `/metrics`, `/.well-known/mowdeck-module` | none | readiness covers the database, NATS and OpenFGA |

Request bodies refuse unknown fields (422). Days are `mon` to `sun`; `start_time` is `HH:MM`.

**Amendment (2026-10-14): the web only displays what the Scheduler decides.** The rain rule (thresholds, lookahead, drying time, when a hold ends) lives in `domain/rain.py` alone. Each decision carries `pause_until`, the first time at which the rule would let a run go ahead (`null` when not holding or when the forecast ends first), and `settings`, the rain pause settings it applied, so the web app (`apps/web/src/lib/mower/rain.ts`) keeps no defaults and no copy of the rule. Change the rule here, with tests, then run `uv run poe openapi` and `pnpm codegen`.

### Module descriptor

Permissions `scheduler.view` (no relation in the model; an account key implied by `mower.view`, the API checks `can_view` on each target) and `schedule.edit` (`can_edit_schedule`, grantable on account, site, group and mower); events `scheduler.run.recorded` and `scheduler.schedule.disabled`; UI area `/schedules` (labels "Zeitpläne" and "Schedules", `required_permission` `mower.view`: every viewer of a mower sees its schedules, the API checks per target).

### Plans

Creating a schedule needs the plan feature `schedules` (403 `urn:mowdeck:problem:feature-not-in-plan`) and stays below the plan limit `max_schedules` (409 `urn:mowdeck:problem:plan-limit` with the limit and the current count in `errors`); both through the Entitlements client of the kit ([ADR 0028](../../docs/adr/0028-entitlements-plans-and-enforcement.md)). A firing refused by Fleet because the plan has no `control` (`not_in_plan`) is recorded as a failed run and does not count towards the permission skips that disable a schedule. `MOWDECK_SCHEDULER_MAX_SCHEDULES_PER_ACCOUNT` stays as an installation ceiling.

## Events and subjects

| Direction | Subject | Kind | Payload | Notes |
| --- | --- | --- | --- | --- |
| produced | `event.scheduler.run.recorded` | JetStream `EVENTS` | `EventEnvelope[ScheduleRunRecorded]` | after the commit of every run (Notifications may use it later) |
| produced | `event.scheduler.schedule.disabled` | JetStream `EVENTS` | `EventEnvelope[ScheduleDisabled]` | the owner lost `can_control`, or the target was removed |
| request | `fleet.targets.resolve` | core NATS (Fleet) | `ResolveTargetRequest` / `ResolveTargetReply` | the target's mowers, time zone and site coordinates |
| request | `fleet.commands.issue` | core NATS (Fleet) | `IssueCommandRequest` / `IssueCommandReply` | `start_mow` and `return_to_dock` on behalf of the owner (or the caller of a manual run) |
| consumed | `telemetry.mower.*` | JetStream, durable and queue `scheduler-states` | `MowerState` | latest activity (busy check) and position |
| consumed | `device.mower.>` | JetStream `DEVICE`, durable and queue `scheduler-device` | `MowerMapSnapshot` | map origin (location) and area ids; capability snapshots are ignored here |
| consumed | `device.mower.*.capabilities` | JetStream `DEVICE`, durable and queue `scheduler-capabilities`, newest message per subject first | `MowerCapabilitiesSnapshot` | what a schedule may set for the mower (check when saved, fit at fire time) |
| consumed | `event.identity.grants.changed` | JetStream, every instance | `GrantsChanged` | drops cached decisions, so a revoked right stops the next firing |

## Execution

Every replica runs a ticker (`MOWDECK_SCHEDULER_TICK_SECONDS`, 30 s). A tick claims one due schedule per transaction (`enabled AND next_run_at <= now`, `FOR UPDATE SKIP LOCKED`), fires it, stores its runs and the next occurrence, commits, and publishes the events; then it follows the open runs (started jobs) the same way: rain while mowing or the end of the window sends the mower back to the dock, a docked state closes the run. Per mower: manual skip, closed window, the owner's `can_control`, busy (`mowing`, `paused`, `returning`), rain, areas, then `fleet.commands.issue` (the schedule's blade height and speed first, then `start_mow`; a refused setpoint ends the run like a refused start). Fleet's answer `forbidden` or `not_found` counts as `skipped_permission`, anything else as `failed`. Metric: `mowdeck_scheduler_runs_total{trigger, outcome}`.

### Weather

`MOWDECK_SCHEDULER_WEATHER_PROVIDER=open_meteo` (default) asks the [Open-Meteo forecast API](https://open-meteo.com/en/docs) for hourly `precipitation`, `precipitation_probability` and `rain` (yesterday to tomorrow, Unix times), no key, 5 s timeout, cached per location (0.01 degree) for 30 minutes and failures for a minute. Free for non-commercial use (600 calls per minute, 5 000 per hour, 10 000 per day); the data is CC BY 4.0, responses with Open-Meteo data carry "Weather data by Open-Meteo.com (CC BY 4.0)". Commercial operators need an Open-Meteo plan (`MOWDECK_SCHEDULER_OPEN_METEO_URL`). `static` answers `dry`, `rain` or `unknown` (tests, development, offline installations). Location: the site's `latitude`/`longitude` (Fleet), else the map's geo origin, else the last reported position; without any the run goes ahead (`no_location`).

## Settings

All settings of `mowdeck-service-kit` apply (`MOWDECK_*`, any value also from `MOWDECK_<NAME>_FILE`). OpenFGA: `MOWDECK_OPENFGA_*` of `mowdeck-authz`; token verification: `MOWDECK_AUTH_*` of `mowdeck-auth`. Service specific:

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_SERVICE_NAME` | `scheduler` | Service name in logs, metrics and traces |
| `MOWDECK_DATABASE_URL` | required | PostgreSQL of the `scheduler` database |
| `MOWDECK_SCHEDULER_TICK_SECONDS` | `30` | How often every replica looks for due schedules and follows started jobs |
| `MOWDECK_SCHEDULER_BATCH_SIZE` | `50` | Schedules one tick fires at most (and started runs it follows) |
| `MOWDECK_SCHEDULER_FLEET_TIMEOUT_SECONDS` | `5` | Timeout of the requests to Fleet |
| `MOWDECK_SCHEDULER_MAX_PERMISSION_SKIPS` | `3` | Firings in a row skipped for missing `can_control` before the schedule is disabled |
| `MOWDECK_SCHEDULER_MAX_SCHEDULES_PER_ACCOUNT` | `100` | 409 above |
| `MOWDECK_SCHEDULER_RUN_RETENTION_DAYS` | `90` | Run records older than this are deleted (advisory lock, one replica) |
| `MOWDECK_SCHEDULER_WEATHER_PROVIDER` | `open_meteo` | `open_meteo` or `static` |
| `MOWDECK_SCHEDULER_STATIC_WEATHER` | `dry` | Answer of the static provider at start |
| `MOWDECK_SCHEDULER_OPEN_METEO_URL` | `https://api.open-meteo.com` | Base URL (a self-hosted instance or a customer URL) |
| `MOWDECK_SCHEDULER_WEATHER_TIMEOUT_SECONDS` | `5` | Timeout of one forecast request |
| `MOWDECK_SCHEDULER_WEATHER_CACHE_SECONDS` | `1800` | How long a forecast is reused per location |
| `MOWDECK_SCHEDULER_DEV_ENDPOINTS` | `false` | Development only: `PUT /api/v1/scheduler/dev/weather` |

## Data model

Tables (Alembic revision `0004`): `schedules` (recurrence, window, areas, pattern (`SchedulePattern` keys), `blade_height_mm`, `speed_mps`, rotation (`rotation_angles`, `rotation_index`, `rotation_step_deg`), rain settings as JSON, owner as `PrincipalRef` JSON, `permission_skips`, `skip_until`, `last_run_at`, `next_run_at`; partial index on `next_run_at` where enabled), `schedule_runs` (one row per mower and attempt with the `angle_deg` a start sent, cascade on schedule delete; partial index of open started runs), `mower_status` (latest activity and position per mower), `mower_places` (map origin and area ids per mower), `mower_capabilities` (newest capabilities per mower), `mow_angles` (the last angle per mower and area) and `mower_rotation` (the step of manual starts per mower). Revision `0003` rewrote stored patterns to `SchedulePattern` keys (legacy `angle` to `angle_deg`, unknown keys dropped); existing schedules keep every new setting unset. Revision `0004` added step rotation (off for every existing schedule) and the two angle tables.

## Layers

| Package | Content |
| --- | --- |
| `domain/` | recurrence (`zoneinfo`), rain decision, validation and rotation rules, entities, errors |
| `application/` | schedules (CRUD, preview, skip), runner (ticker, manual runs, follow-up), weather (location and decision), ingest, access checks, ports |
| `adapters/api/` | routes, schemas, error mapping |
| `adapters/bus/` | Fleet gateway (core NATS requests), event publisher, subscriptions |
| `adapters/db/` | tables, the PostgreSQL store, Alembic migrations |
| `adapters/weather/` | Open-Meteo and static providers |
| `adapters/memory.py` | the memory store (tests) |
| `main.py` | wiring (`build_app`), ticker and retention tasks, `main()` |

## Local start

With the Compose stack (builds the image, runs the migrations, waits for health):

```bash
cp infra/compose/.env.example infra/compose/.env   # SCHEDULER_WEATHER_PROVIDER=static offline
docker compose -f infra/compose/docker-compose.yml up -d --build --wait
curl -X POST http://localhost/api/v1/scheduler/schedules -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name": "Morning", "target_type": "mower",
  "target_id": "<mower_id>", "kind": "weekly", "days_of_week": ["mon", "thu"],
  "start_time": "09:00", "area_ids": ["front"], "rotation_angles": [0, 90]}'
curl http://localhost/api/v1/scheduler/schedules/<id>/preview -H "Authorization: Bearer $TOKEN"
curl http://localhost/api/v1/scheduler/schedules/<id>/runs -H "Authorization: Bearer $TOKEN"
```

Container image (build context is the repository root):

```bash
docker build -f services/scheduler/Dockerfile -t mowdeck/scheduler:dev .
```

## Tests

```bash
uv run pytest services/scheduler --cov=scheduler
```

`tests/scheduler_support.py` builds the Scheduler *and* the real Fleet app on one in-memory bus and one in-memory OpenFGA fake, with a fake Device-Gateway, a manual clock and the static weather provider, so firings are tested down to Fleet's command record. The Open-Meteo client is tested against the recorded answer `tests/data/open_meteo_berlin.json` (no network). PostgreSQL tests (migrations up and down, drift, the store contract, `SKIP LOCKED`) run with `MOWDECK_TEST_DATABASE_URL`, the NATS round trip with `MOWDECK_TEST_NATS_URL`:

```bash
docker run -d --name scheduler-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test postgres:17.11-alpine
docker run -d --name scheduler-test-nats -p 4222:4222 nats:2.15.0-alpine -js
MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \
MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/scheduler
docker rm -f scheduler-test-pg scheduler-test-nats
```

Against the real Open-Meteo API (by hand, never in CI): `uv run python services/scheduler/scripts/open_meteo_live.py --live [--lat 48.14 --lon 11.58] [--record file.json]`.

## Limits

- Group schedules mow all areas of each mower and set only what every member offers; per-mower areas and settings, planner routes and maintenance reminders follow later.
- The map snapshot consumer starts at new messages: a Scheduler deployed after a mower's map was published learns its areas and origin only with the next snapshot (the Device-Gateway publishes a map when it differs from the one it published before, and again after its own restart). Site coordinates and explicit `area_ids` have no such gap.
- A crash between Fleet accepting a start and the commit of the run can start the mower a second time at the next tick (rare; the command expires after 30 s).
- The dev weather endpoint switches only the replica that answers.
