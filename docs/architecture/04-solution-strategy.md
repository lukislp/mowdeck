# 4. Solution strategy

| Goal | Approach |
| --- | --- |
| Tenant isolation | Account ID only from the verified token; filter in every query; per-service tests that request foreign data; OpenFGA for permission checks. |
| Independent scaling and failure | Small services; synchronous queries via the gateway, everything time-critical and all commands asynchronous over NATS JetStream. No service reads another service's database; only contracts are shared. |
| Same everywhere | Identical container images for Compose and Kubernetes; differences only in configuration and instance count. |
| Safe commands | Commands carry a unique ID (deduplication) and an expiry (stale "start" is dropped); emergency stop has priority; dead-man switch for driving. |
| Extensibility | Fixed extension points (interfaces) and self-describing modules; shared libraries; a service template. Every feature sits behind a feature key. |
| Authentication | Identity issues short-lived signed JWTs (EdDSA); services verify them locally using a JWKS public key, with no per-request call to Identity. |
| Authorization | OpenFGA with a model stored in the repository (ADR 0001). |
| Contracts | OpenAPI for REST, AsyncAPI/JSON Schema for events, in the package `mowdeck-contracts`. |
| Internal structure | Every service has the same layers: domain, application (use cases), adapters. |
| Observability | OpenTelemetry traces and logs, Prometheus metrics, a correlation ID through all services. |

## Technology stack

| Layer | Choice |
| --- | --- |
| Services | Python 3.13, FastAPI |
| Mower control | `mowdeck-control` library (interface, safety controller, simulator, [ADR 0012](../adr/0012-mower-control-interface-and-safety-rules.md)); PyMammotion adapters for BLE bridge and cloud in the GPL-3.0 package `mowdeck-control-mammotion` |
| Bridge | ESP32 (for example XIAO ESP32-S3) |
| Gateway | Traefik (ADR 0008) |
| Event bus | NATS JetStream (ADR 0007) |
| Database | PostgreSQL, one database per service, Alembic migrations; optional TimescaleDB (ADR 0006) |
| Authentication | Plug-ins: password (Argon2id), API tokens, OAuth 2/OIDC, later passkeys |
| Service tokens | JWT with EdDSA, public keys via JWKS |
| Authorization | OpenFGA (ADR 0001) |
| Payment | Stripe adapter in Billing, switchable |
| Frontend | SvelteKit PWA, Leaflet with Geoman (ADR 0003) |
| Store app (later) | Capacitor |
| Route planning | Shapely |
| Tooling | uv workspace, Ruff, mypy strict, pytest, MkDocs Material (ADR 0004) |
| Operations | Docker Compose, Helm chart (cloud), images built in CI |
