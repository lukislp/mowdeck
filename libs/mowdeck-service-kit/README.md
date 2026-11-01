# mowdeck-service-kit

The skeleton every MowDeck service is built on: configuration, structured logging, correlation ids, a uniform error format, health and metrics endpoints, the module descriptor, the NATS JetStream event bus, tenant isolation helpers and test utilities. Licence: Apache-2.0.

Extras: `otel` (OpenTelemetry tracing), `db` (async SQLAlchemy with asyncpg), `testing` (httpx for the test client helpers).

## A minimal service

```python
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI
from mowdeck_contracts.v1 import (
    EventEnvelope,
    ModuleDescriptor,
    PermissionDef,
    Principal,
    UiAreaDef,
)
from mowdeck_service_kit import (
    Bus,
    CurrentAccountId,
    NotFound,
    ServiceSettings,
    create_app,
    require_permission,
    run,
)
from mowdeck_service_kit.bus import publish_event


class FleetSettings(ServiceSettings):
    service_name: str = "fleet"
    max_mowers: int = 50
    """Maximum number of mowers per account."""


DESCRIPTOR = ModuleDescriptor(
    name="fleet",
    version="0.1.0",
    permissions=[PermissionDef(key="mower.view", description="View mowers")],
    ui_areas=[UiAreaDef(route="/mowers", menu_label="Mowers", required_permission="mower.view")],
)

router = APIRouter(prefix="/mowers", tags=["mowers"])
CanView = Annotated[Principal, Depends(require_permission("mower.view"))]


@router.get("/{mower_id}")
async def get_mower(mower_id: UUID, account_id: CurrentAccountId, _: CanView) -> dict[str, str]:
    mower = await repository.get(account_id=account_id, mower_id=mower_id)  # filtered by account
    if mower is None:
        raise NotFound("mower not found")
    return {"id": str(mower_id)}


@router.post("/{mower_id}/register", status_code=202)
async def register(mower_id: UUID, account_id: CurrentAccountId, bus: Bus) -> None:
    await publish_event(
        bus,
        EventEnvelope[dict[str, str]](
            type="mower.registered",
            producer="fleet",
            account_id=account_id,
            payload={"mower_id": str(mower_id)},
        ),
    )


async def open_resources(app: FastAPI) -> None:
    app.state.repository = ...  # e.g. create the engine, warm caches


async def close_resources(app: FastAPI) -> None: ...  # dispose the engine


def main() -> None:
    settings = FleetSettings.from_env()
    app = create_app(
        settings,
        DESCRIPTOR,
        routers=[router],
        on_startup=[open_resources],
        on_shutdown=[close_resources],
        # principal_resolver=JwtPrincipalResolver(...)  # from mowdeck-auth (later task)
    )
    run(app, settings)
```

`create_app` wires, in this order:

1. Logging (`configure_logging`) for JSON lines on stdout, or console output in `dev`.
2. Middleware: correlation id (outermost), generic 500 handler, HTTP metrics.
3. Exception handlers rendering `application/problem+json`.
4. Operational endpoints `/healthz`, `/readyz`, `/metrics`, `/.well-known/mowdeck-module` (outside `/api/v1`).
5. Your routers under `/api/v1` (HTTP and WebSocket routes; the kit's middlewares pass `websocket` scopes through untouched); OpenAPI at `/api/v1/openapi.json`. There is no Swagger UI or ReDoc page (FastAPI loads them from a CDN); the interactive explorer is the web app's `/api-explorer`, self-hosted and off by default ([ADR 0072](../../docs/adr/0072-api-explorer-in-the-web-app.md)).
6. OpenTelemetry instrumentation when `MOWDECK_OTEL_EXPORTER_OTLP_ENDPOINT` is set.
7. Lifespan: connect the bus (a `JetStreamBus` when `nats_enabled`, unless you pass `bus=`), add the `nats` readiness check, run `on_startup` hooks. On shutdown: mark the service as draining (`/readyz` returns 503), run `on_shutdown` hooks within `shutdown_grace_seconds`, then drain and close the bus, then flush traces.

`run(app, settings)` serves the app with uvicorn (no access log config of its own, proxy headers, graceful shutdown timeout from the settings). Extra keyword arguments go to `uvicorn.Config`, for example `ws_ping_interval`, `ws_ping_timeout` and `ws_max_size` for WebSocket services; `server_class=` takes a `uvicorn.Server` subclass for services that must act before uvicorn closes the open connections on shutdown (Realtime closes its sockets with 1001 first).

State lives on `app.state`: `settings`, `descriptor`, `bus`, `metrics_registry`, `readiness_checks`, `principal_resolver`, `draining`. Register extra readiness checks with `add_readiness_check(app, check)` (for example from an `on_startup` hook) and extra Prometheus metrics on `app.state.metrics_registry`.

## Settings reference

All settings come from environment variables with the prefix `MOWDECK_`. Any setting can be read from a file instead by setting `MOWDECK_<NAME>_FILE` (Docker and Kubernetes secrets); setting both is an error. Subclasses of `ServiceSettings` follow the same rules. Load settings with `MySettings.from_env()`.

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_SERVICE_NAME` | required | Service name for logs, metrics and traces (lower-case, `a-z0-9-`) |
| `MOWDECK_ENVIRONMENT` | `prod` | `dev`, `test` or `prod`; `dev` switches logs to the console renderer |
| `MOWDECK_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (case insensitive) |
| `MOWDECK_LOG_FORMAT` | `auto` | `json`, `console` or `auto` (console in `dev`, JSON elsewhere) |
| `MOWDECK_HTTP_HOST` | `0.0.0.0` | Interface the HTTP server binds to |
| `MOWDECK_HTTP_PORT` | `8000` | HTTP port |
| `MOWDECK_NATS_URL` | `nats://localhost:4222` | NATS server URL(s), comma separated |
| `MOWDECK_NATS_ENABLED` | `true` | Connect to NATS on startup |
| `MOWDECK_NATS_USER` | unset | The service's NATS user ([ADR 0073](../../docs/adr/0073-nats-authentication-and-subject-permissions.md)); unset: no credentials (development, tests). Also the inbox prefix `_INBOX.<user>` |
| `MOWDECK_NATS_PASSWORD` | unset | Its password (secret; usually `MOWDECK_NATS_PASSWORD_FILE`, never logged); together with the user or not at all |
| `MOWDECK_NATS_MANAGE_STREAMS` | unset | Create and update the JetStream streams on connect; unset: only without a user (with credentials the `nats-streams` job does it) |
| `MOWDECK_NATS_STREAM_WAIT_SECONDS` | `120` | How long a service that does not manage the streams waits for them on connect |
| `MOWDECK_DATABASE_URL` | unset | PostgreSQL URL (secret; usually `MOWDECK_DATABASE_URL_FILE`) |
| `MOWDECK_OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP/HTTP collector, e.g. `http://otel-collector:4318`; tracing is off when unset |
| `MOWDECK_CLIENT_IP_DEPTH` | unset | Trusted proxies in front of the gateway that set or append `X-Forwarded-For`; `client_address.client_address()` takes the entry that many places left of the gateway's own (unset: the leftmost entry). Compose and Helm pass `GATEWAY_CLIENT_IP_DEPTH`. Rate limits key `client_address.limiter_key()` of that address (IPv6 per /64, IPv4 unchanged); audit logs keep the whole address |
| `MOWDECK_METRICS_ENABLED` | `true` | Serve Prometheus metrics at `/metrics` |
| `MOWDECK_SHUTDOWN_GRACE_SECONDS` | `10` | Budget for shutdown hooks; also uvicorn's graceful shutdown timeout |
| `MOWDECK_ENTITLEMENTS_BILLING_ENABLED` | `false` | Installation switch of the plans (`billing.enabled`); the Entitlements client's fallback when Entitlements does not answer (off: allowed, on: refused) |
| `MOWDECK_ENTITLEMENTS_TIMEOUT_SECONDS` | `1` | Timeout of one `entitlements.check` request |
| `MOWDECK_ENTITLEMENTS_CACHE_SECONDS` | `60` | How long an account's plan snapshot is reused (0: no cache) |
| `MOWDECK_VERSION` | unset | Version of the build; the release build bakes it into the image (unset: a development build) |
| `MOWDECK_SOURCE_URL` | `https://github.com/lukislp/mowdeck` | Repository of the running code; a fork that runs modified code sets its own (AGPL-3.0 section 13). `{ref}` makes it a template for hosts other than GitHub |
| `MOWDECK_SOURCE_REF` | unset | Git ref of the build (`v<version>` of a release, a commit of a test build); unset: `v<version>` for a semantic version |
| `MOWDECK_DEMO_MODE` | `false` | Public demo ([ADR 0068](../../docs/adr/0068-public-demo-mode.md)): `X-MowDeck-Demo: 1` on every response and the service's demo policy refuses its unsafe actions (403 `demo-mode`). Never on an installation with real mowers |
| `MOWDECK_DEMO_SEED_TOKEN` | unset | Secret of the demo seed job: with it in `X-MowDeck-Demo-Seed` a request passes the demo policy and may call the seed endpoints (only in demo mode) |

### Source code header (AGPL-3.0 section 13)

In demo mode `create_app` adds `DemoModeMiddleware` (`mowdeck_service_kit.demo`) with the service's `demo_policy`: a `DemoPolicy` of refused paths (`blocked`) or of the only allowed mutations (`allow_only`); safe methods always pass. `require_demo` and `require_demo_seed` are the dependencies of demo-only endpoints.

Every HTTP response carries `X-Source-Code` with the address of the source of the running build (`mowdeck_service_kit.source`): `<MOWDECK_SOURCE_URL>/tree/<ref>` with the ref from `MOWDECK_SOURCE_REF` or `v<MOWDECK_VERSION>`, and the repository itself for a development build. The web app shows the same link in its footer and under Settings, About (LICENSING.md).

Every HTTP response also carries `X-Content-Type-Options: nosniff`, and a response to a request with an `Authorization` header gets `Cache-Control: no-store` unless the route set its own `Cache-Control` (aerial tiles, error lexicons and other reusable answers keep theirs; `mowdeck_service_kit.hardening`). WebSockets are untouched.

The field docstrings in `settings.py` are the source of this table and become the generated configuration reference later.

## Log redaction

`configure_logging` installs the `redact_secrets` processor for every service, right before the renderer (also for standard library records). It masks the value of secret keys with `***` (`None` stays `None`), case-insensitively and at any depth of dicts, lists and tuples, including `connection_options`: `password`, `psk`, `noise_psk`, `secret`, `token`, `api_key`, `apikey`, `authorization`, `credential(s)`, `private_key`, `client_secret`, `refresh_token`, `access_token`, `*_password`, `*_secret`, `*_token` and `*_key` names containing `secret`, `api`, `private` or `encryption`. Ids such as `product_key` stay readable. Caller-owned dicts are copied, never changed.

Only plain dicts, lists and tuples are inspected. Pydantic models and dataclasses are not converted, so never log a raw registration or credential model; log selected fields instead.

## Event bus

```python
from mowdeck_service_kit.bus import BusMessage, DropMessage, publish_command
from mowdeck_contracts.v1 import CommandEnvelope
from mowdeck_contracts.v1.subjects import ALL_COMMANDS


async def on_command(message: BusMessage) -> None:
    command = message.model(CommandEnvelope)
    if command.is_expired():
        raise DropMessage("expired")  # terminate, no redelivery
    await execute(command)  # raising anything else means: redeliver


await bus.subscribe(ALL_COMMANDS, on_command, durable="device-gateway")
await publish_command(bus, envelope)  # msg id = command_id, de-duplicated by JetStream
```

`EventBus` is a Protocol with `publish`, `subscribe`, `request`, `close` and `is_connected`. Two implementations share the same semantics:

- `JetStreamBus` (nats-py): ensures the streams (`COMMANDS`, `TELEMETRY`, `EVENTS`, `DEVICE`, `CAMERA`) from `mowdeck-contracts` on connect, or, without `manage_streams`, waits for them; subjects captured by a stream are published to JetStream with the `Nats-Msg-Id` header, all others (and `request`) use core NATS; reconnects forever; durable or queue consumers with explicit acks, `max_deliver` (default 5) and a short nak delay. Stream consumers start at new messages; `subscribe(..., last_per_subject=True)` makes a new consumer start with the newest stored message of every matching subject (JetStream `DeliverPolicy.LAST_PER_SUBJECT`), for last-value streams such as `DEVICE` (the in-memory bus replays them the same way).
- `InMemoryBus`: delivers synchronously inside `publish`, de-duplicates by msg id within each stream's duplicate window, supports queue groups, redelivery and request/reply; records `published` and `dropped` messages for assertions.

**NATS credentials and least privilege** ([ADR 0073](../../docs/adr/0073-nats-authentication-and-subject-permissions.md)). `JetStreamBus.from_settings` connects as `MOWDECK_NATS_USER` with its password and the inbox prefix `_INBOX.<user>`, the only inbox the server lets the user read. Stream consumers are created through the subject based API (`$JS.API.CONSUMER.CREATE.<stream>.<name>.<filter>`, ephemeral ones named `eph-<random>`), so the server's permissions limit the filters a service may consume; a durable consumer delivers to `_INBOX.<user>.durable.<stream>.<name>` (the same for every replica of a queue group), and one created earlier with another delivery subject is moved there on subscribe, keeping its position. With a user the service does not change the streams: `python -m mowdeck_service_kit.bus.streams` (the `nats-streams` job, user `streams`) creates and updates them, and the services wait up to `MOWDECK_NATS_STREAM_WAIT_SECONDS`. The users and their subjects are generated from `tools/repo-checks/scripts/nats_auth.py`; a new bus subject of a service belongs there.

Handler rules: return normally to acknowledge, raise to get a redelivery, raise `DropMessage` to discard. New subscriptions receive new messages only. The correlation id travels in the `X-Correlation-ID` header and is restored while a handler runs. Helpers: `publish_event` (subject `event.<domain>.<name>`, msg id = event id), `publish_command` (refuses expired commands, adds `Mowdeck-Priority`), `publish_command_result`.

## Error format

Every error response is an RFC 9457 problem document with media type `application/problem+json`:

```json
{
  "type": "urn:mowdeck:problem:not-found",
  "title": "Not found",
  "status": 404,
  "detail": "mower not found",
  "instance": "/api/v1/mowers/6f1c...",
  "correlation_id": "0b6c4c1e-..."
}
```

Raise `BadRequest` (400), `Unauthorized` (401, adds `WWW-Authenticate: Bearer`), `Forbidden` (403), `NotFound` (404), `Conflict` (409), `ValidationFailed` (422, with `errors=[ProblemFieldError(...)]`) or `ServiceUnavailable` (503); subclass `ServiceError` for new kinds. Request validation errors become 422 with one entry per field (`pointer` for body fields, `parameter` for query, path and header values; input values are never echoed). Unknown routes and wrong methods become 404 and 405 problems. Unexpected exceptions become a generic 500 without internals and are logged with the stack trace and correlation id.

## Tenancy rules

- The account id only ever comes from the verified principal (`CurrentAccountId`), never from the request body, query string or path.
- Every account-scoped query filters by that account id.
- Resources of other accounts are answered with 404, not 403, so their existence is not revealed.
- `current_principal` returns 401 without a principal; `current_account_id` returns 403 for principals without an account (operators, services).
- `require_permission("mower.control")` asks the `PermissionChecker` on `app.state.permission_checker` when one is installed, otherwise it checks the token level permission list. `mowdeck_authz.fastapi.install_authz(app, authorizer)` installs a checker that asks OpenFGA for the key's computed relation on the principal's account (`mower.control` becomes `can_control` on `account:<acc>`). A checker is any object with `async def has_permission(principal, permission) -> bool` (protocol `PermissionChecker`). Object-level checks use `mowdeck_authz.fastapi.require(...)` or `require_or_not_found(...)`.
- The principal comes from a `PrincipalResolver` (`async resolve(request) -> Principal`). The default resolver rejects everything with 401; `mowdeck-auth` provides the JWT resolver later.

## Database (extra `db`)

```python
from mowdeck_service_kit.db import create_engine, readiness_check_db, session_dependency

engine = create_engine(settings)  # postgresql:// becomes postgresql+asyncpg://
get_session = session_dependency(engine)  # commit on success, rollback on error
app = create_app(settings, DESCRIPTOR, readiness_checks=[readiness_check_db(engine)])
```

Migrations are not part of the kit; the service template runs Alembic as a separate step before start.

## Plans (Entitlements client)

`mowdeck_service_kit.entitlements` enforces the account's plan with one call per enforcement point ([ADR 0028](../../docs/adr/0028-entitlements-plans-and-enforcement.md)):

```python
from mowdeck_service_kit.entitlements import client_from_settings, install_entitlements

plans = client_from_settings(settings)  # EntitlementsClient, bus attached at startup
await plans.require_limit(account_id, "max_mowers", current=count)  # 409 plan-limit
await plans.require_feature(account_id, "manual_drive")  # 403 feature-not-in-plan
```

The client asks `entitlements.check` (core NATS) once per account and caches the returned `EntitlementSnapshot` for `MOWDECK_ENTITLEMENTS_CACHE_SECONDS`; `await plans.subscribe()` drops snapshots on `entitlements.plan.changed`. When Entitlements does not answer, `MOWDECK_ENTITLEMENTS_BILLING_ENABLED` decides (off: allowed with plan `unlimited`, on: refused with `entitlements_unavailable`); outages are never cached. `view` and `emergency_stop` are always allowed and never asked. The problem types are `PlanLimitReached` (409 `plan-limit`, `errors` carry `limit` and `current`) and `FeatureNotInPlan` (403 `feature-not-in-plan`); `feature_gate(key)` is a FastAPI dependency for routes, after `install_entitlements(app, plans)`. Tests use `mowdeck_service_kit.testing.FakeEntitlements` (one plan for every account, changeable between requests, records every question).

## Testing utilities

`mowdeck_service_kit.testing` (extra `testing`) provides `make_test_app(...)` (in-memory bus, static principal, settings independent of the environment), `make_test_settings`, `make_test_descriptor`, `make_principal`, `StaticPrincipalResolver` and `lifespan_client(app)`, an `httpx.AsyncClient` that also runs startup and shutdown hooks.

Cross-tenant rejection pattern (mandatory for every endpoint touching account data):

```python
async def test_cross_tenant_access_is_rejected() -> None:
    owner = make_principal(permissions=["mower.view"])
    intruder = make_principal(permissions=["mower.view"])  # different account
    mower_id = await create_mower(owner)
    resolver = StaticPrincipalResolver(owner)
    app = make_test_app(routers=[router], principal_resolver=resolver)
    async with lifespan_client(app) as client:
        assert (await client.get(f"/api/v1/mowers/{mower_id}")).status_code == 200
        resolver.principal = intruder
        assert (await client.get(f"/api/v1/mowers/{mower_id}")).status_code == 404
        smuggled = await client.get(
            f"/api/v1/mowers/{mower_id}", params={"account_id": str(owner.account_id)}
        )
        assert smuggled.status_code == 404
```

## Development

```bash
uv run pytest libs/mowdeck-service-kit --cov=mowdeck_service_kit
```

The JetStream integration tests run only when `MOWDECK_TEST_NATS_URL` is set:

```bash
docker run -d --name mowdeck-test-nats -p 4222:4222 public.ecr.aws/docker/library/nats:2.15.0-alpine -js
MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest libs/mowdeck-service-kit
docker rm -f mowdeck-test-nats
```

The generated NATS permissions are tested against a real server by `tools/repo-checks/tests/test_nats_auth_server.py` when `MOWDECK_TEST_NATS_AUTH_URL` points at a NATS that runs `infra/compose/nats/auth.conf` (the command is in that file's docstring).

An optional PostgreSQL test runs when `MOWDECK_TEST_DATABASE_URL` is set (for example `postgresql://u:p@127.0.0.1:5433/db`).
