# entitlements

Plans, limits and feature gates: answers whether an account may use a feature, or have one more of something ([ADR 0028](../../docs/adr/0028-entitlements-plans-and-enforcement.md)).

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; the layer rules and the definition of done are in `docs/dev/service-template.md`.

## Purpose

Billing handles money ([ADR 0031](../../docs/adr/0031-billing-with-stripe-adapter.md)), Entitlements what an account may do. It owns the plans (data seeded from `infra/entitlements/plans.yaml`), the plan of every account with its status (`active`, `trial`, `grace`, `expired`) and history, and the feature catalogue collected from the module descriptors. Other services ask over core NATS (`entitlements.check`) through the `EntitlementsClient` of `mowdeck-service-kit`, which caches one snapshot per account. It scales with requests; the clients' cache keeps them few.

**Installation switch.** `MOWDECK_ENTITLEMENTS_BILLING_ENABLED=false` (the default): every account resolves to the plan `unlimited` (no limits, every feature), whatever is assigned, and the API says so (`billing_enabled: false`). `true`: the account's assignment applies; accounts without one get `MOWDECK_ENTITLEMENTS_DEFAULT_PLAN` (default `home`).

**Statuses.** `trial` behaves like `active` until `trial_until`, then counts as `expired`; `grace` (a payment problem, set by Billing or by hand) ends as `expired` at `grace_until`. In `grace` and `expired` only `view` and `emergency_stop` remain and nothing new may be created; no mower is ever stopped by a plan change.

## Plan file

`infra/entitlements/plans.yaml` (the image ships it at `/etc/mowdeck/entitlements/plans.yaml`; Compose mounts the repository copy there) is read at every start and upserted by key:

```yaml
plans:
  - key: home              # lower case, digits, '-' and '_'
    name: Home
    description: One garden with up to four mowers.
    is_default: true       # at most one plan; used when the default plan setting names none
    trial_days: 30         # new accounts on the default plan start a trial
    active: true           # false: no longer offered, accounts keep it
    limits:                # omitted or null: unlimited; unknown names are rejected
      max_mowers: 4
      max_users: 5
      max_sites: 1         # the hidden default site counts
      max_groups: 2
      max_schedules: 10
      history_days: 90
    features: [view, control, emergency_stop, schedules, notifications, manual_drive]
```

- A plan is rewritten only when its file entry changed since the last start (`file_digest`); an edit through `PUT /plans/{key}` survives restarts until then. Plans are never deleted.
- Feature keys are hierarchical (`notifications` grants `notifications.push`); `*` grants every feature. Keys no deployed module declares are kept and logged once the catalogue is complete (a plan file may lead a deployment); `GET /plans/{key}` lists them as `unknown_features`.
- A missing file leaves only the built-in plan `unlimited`; an invalid file stops the start with a readable message.

Shipped plans: `unlimited` (`*`, no limits), `home` (limits above; `view`, `control`, `emergency_stop`, `schedules`, `notifications`, `manual_drive`, `home_assistant`, `map`, `health`; 30 trial days, default), `fleet` (no limits; the home features plus `fleet.sites`, `fleet.groups`, `fleet.bulk_commands`, `reports`, `api_tokens.scoped`; 14 trial days).

## Interfaces

All routes are mounted under `/api/v1` (OpenAPI at `/api/v1/openapi.json`). Traefik routes `/api/v1/entitlements` to this service. Operator: `can_manage_plans` on `platform:main` in OpenFGA (`python -m identity.cli grant-platform-admin USERNAME`); account owners get 403.

| Method | Path | Who | Description |
| --- | --- | --- | --- |
| GET | `/api/v1/entitlements/me` | any principal of an account | The plan that applies to the token's account now: `plan`, `plan_name`, `status` (effective), `source`, `billing_enabled`, `limits`, `features`, `since`, `trial_until`, `grace_until` |
| GET | `/api/v1/entitlements/features` | any principal | The feature catalogue: `key`, `description`, `module`, `retired` |
| GET | `/api/v1/entitlements/plans` | public | The offered (active) plans |
| GET | `/api/v1/entitlements/plans/{key}` | operator | One plan, also inactive, with `unknown_features` and `from_plan_file` |
| PUT | `/api/v1/entitlements/plans/{key}` | operator | Create or replace a plan (the whole plan); every client drops its cached snapshots |
| GET | `/api/v1/entitlements/accounts?offset&limit` | operator | Accounts with an assignment, oldest first: `assigned` (stored) and `effective` (what applies); no mower data |
| GET | `/api/v1/entitlements/accounts/{account_id}` | operator | One account: effective plan, assignment, history (newest first) |
| PUT | `/api/v1/entitlements/accounts/{account_id}/plan` | operator | Manual assignment `{plan, status, trial_until?, grace_until?, reason, account_name?}` (`source=manual`): history row with the operator, `entitlements.plan.changed`; a trial without `trial_until` lasts the plan's trial days, a grace period without `grace_until` lasts `MOWDECK_ENTITLEMENTS_GRACE_DAYS` |
| GET | `/healthz`, `/readyz`, `/metrics` | none | Liveness, readiness (bus, database, OpenFGA), Prometheus metrics |
| GET | `/.well-known/mowdeck-module` | none | Module descriptor: the core features `view`, `control`, `emergency_stop`; no permissions |

## Events and subjects

| Direction | Subject | Kind | Payload | Notes |
| --- | --- | --- | --- | --- |
| reply | `entitlements.plan.set` | core NATS, queue `entitlements` (internal, Billing only) | `PlanSetRequest{account_id, plan, status, trial_until?, grace_until?, reason, account_name?, reference?}` / `PlanSetReply{ok, plan, status, changed, error?}` | sets the plan with `source=billing`: history row (actor `service:billing:<reference>`), `entitlements.plan.changed`; a trial without `trial_until` lasts the plan's trial days, a grace period without `grace_until` `MOWDECK_ENTITLEMENTS_GRACE_DAYS`; a request equal to the stored row changes nothing; unknown plan: `unknown_plan`, invalid request: `invalid_request` |
| reply | `entitlements.check` | core NATS, queue `entitlements` (internal) | `EntitlementCheckRequest{account_id, feature \| limit: {key, current}}` / `EntitlementCheckReply{allowed, plan, status, reason, limit, current, snapshot}` | neither feature nor limit: names the plan (Identity's `plan` claim); counted as `mowdeck_entitlements_checks_total{kind, outcome}` |
| produced | `event.entitlements.plan.changed` | JetStream `EVENTS` | `EventEnvelope[PlanChanged]` | one account on manual assignment; `all_accounts` on plan edits and at every start |
| consumed | `event.identity.account.created` | JetStream, durable and deliver group `entitlements-accounts` | `EventEnvelope[AccountCreated]` | gives the account the default plan (a trial when the plan has trial days); idempotent |

Reasons of a refusal: `feature_not_in_plan`, `limit_reached`, `plan_inactive` (grace or expired), and on the client side `entitlements_unavailable`.

## Enforcement in the other services

| Service | Check |
| --- | --- |
| Fleet | `max_mowers`, `max_sites`, `max_groups` (409 `plan-limit`); `control` for every command except the emergency stop, `fleet.bulk_commands` for bulk commands (403 `feature-not-in-plan`) |
| Scheduler | `schedules` and `max_schedules` on create |
| Identity | `max_users` on sub-user creation; the token's `plan` claim |
| Realtime | `manual_drive` on `drive_start` (`drive_not_allowed`) |
| Notifications | `notifications.push` on push subscriptions |

## Settings

All settings of `mowdeck-service-kit` apply (`MOWDECK_*`, any value also from `MOWDECK_<NAME>_FILE`). OpenFGA: `MOWDECK_OPENFGA_*` of `mowdeck-authz`; token verification: `MOWDECK_AUTH_*` of `mowdeck-auth` (`MOWDECK_AUTH_JWKS_URL` unset means every request is 401).

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_SERVICE_NAME` | `entitlements` | Service name in logs, metrics and traces |
| `MOWDECK_DATABASE_URL` | required | PostgreSQL of the `entitlements` database |
| `MOWDECK_ENTITLEMENTS_BILLING_ENABLED` | `false` | Kit setting, the installation switch: off, every account has the plan `unlimited`; on, the plans apply (Compose: `ENTITLEMENTS_BILLING_ENABLED`) |
| `MOWDECK_ENTITLEMENTS_DEFAULT_PLAN` | `home` | Plan of accounts without an assignment and of new accounts; unknown: the plan marked `is_default` (Compose: `ENTITLEMENTS_DEFAULT_PLAN`) |
| `MOWDECK_ENTITLEMENTS_PLANS_FILE` | `/etc/mowdeck/entitlements/plans.yaml` | The plan file seeded at start |
| `MOWDECK_ENTITLEMENTS_GRACE_DAYS` | `14` | Length of a grace period assigned without `grace_until` |
| `MOWDECK_ENTITLEMENTS_MODULE_URLS` | empty | Comma separated base URLs whose module descriptors declare features |
| `MOWDECK_ENTITLEMENTS_CATALOGUE_REFRESH_SECONDS` | `600` | How often the descriptors are fetched again (30 s while one is missing) |

## Data model

`plans` (key, name, description, `limits` JSONB, `features` JSONB, trial days, `is_default`, `active`, `file_digest`), `account_plans` (account id, account name, plan key, status, since, trial and grace ends, source), `plan_history` (every assignment with reason and actor), `features` (the catalogue). Migrations: `0001` (empty), `0002` (the schema); a drift test compares them with the metadata.

## Layers

| Package | Content | Rule |
| --- | --- | --- |
| `domain/` | plans, account plans, the plan file parser, the resolution rules (status at a moment) | pure Python, no I/O |
| `application/` | `EntitlementsService` (seed, resolve, check, assign, accounts), `CatalogueService`, ports | depends on `domain` and ports only |
| `adapters/api/` | FastAPI routers, schemas, the operator gate | translate HTTP to use cases |
| `adapters/bus/` | `entitlements.check`, `identity.account.created`, the event publisher | translate messages to use cases |
| `adapters/db/` | SQLAlchemy Core store, Alembic migrations; `adapters/memory.py` for tests | implement the store port |
| `main.py` | wiring (`build_app`) and `main()` | the only place that knows every layer |

## Local start

```bash
uv sync
docker compose -f infra/compose/docker-compose.yml up -d --wait nats postgres openfga
MOWDECK_DATABASE_URL=postgresql://entitlements:<password>@127.0.0.1:5432/entitlements \
  uv run python -m entitlements.adapters.db.migrate
MOWDECK_ENVIRONMENT=dev MOWDECK_ENTITLEMENTS_PLANS_FILE=infra/entitlements/plans.yaml \
  MOWDECK_DATABASE_URL=postgresql://entitlements:<password>@127.0.0.1:5432/entitlements \
  uv run python -m entitlements.main
curl http://127.0.0.1:8000/api/v1/entitlements/plans
```

Container image (build context is the repository root): `docker build -f services/entitlements/Dockerfile -t mowdeck/entitlements:dev .` The Compose blocks are `entitlements` and `entitlements-migrate` in `infra/compose/docker-compose.yml`, Kubernetes uses the umbrella chart `infra/helm/mowdeck` (`templates/entitlements.yaml`; the plan file is a ConfigMap built from `infra/helm/mowdeck/files/plans.yaml`, an identical copy of `infra/entitlements/plans.yaml` that a repository check keeps in sync).

## Tests

`uv run pytest services/entitlements` runs the domain, API, bus and store tests with the memory store, the in-memory OpenFGA fake and a manual clock. Integration tests run when their URL is set: PostgreSQL (`MOWDECK_TEST_DATABASE_URL`: migrations up and down, drift, the store contract), NATS (`MOWDECK_TEST_NATS_URL`: `entitlements.check` and `entitlements.plan.changed` with the kit client) and OpenFGA (`MOWDECK_TEST_OPENFGA_URL`: `platform:main#can_manage_plans`).
