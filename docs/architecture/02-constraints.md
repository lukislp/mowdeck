# 2. Constraints

## Technical constraints

| Constraint | Background |
| --- | --- |
| PyMammotion is the only mower protocol implementation | The Mammotion protocol is unofficial; a firmware update can break features at any time. Pin the version and update firmware only after community feedback. |
| Bluetooth is the default link | WLAN control is announced but not available. Bluetooth runs through an ESP32 bridge that connects outbound to the Device-Gateway. |
| Python 3.13 | One language for services, PyMammotion (requires 3.13 or newer) and Home Assistant. See ADR 0010. |
| PostgreSQL only | No SQLite (see ADR 0006). |
| Only portable open components | PostgreSQL, S3-compatible storage, OIDC, NATS. No provider-specific cloud services. |
| Images for amd64 and arm64 | Mini PC, Raspberry Pi 5 and cloud. |

## Organizational constraints

| Constraint | Background |
| --- | --- |
| Licensing | AGPL-3.0 for services and apps, Apache-2.0 for libraries (ADR 0005). |
| Language | All code, comments, commits and documentation are English; UI is German by default plus English. |
| Process | One task at a time, each with tests and README; definition of done includes docs and ADRs. |
| Terms of use | Uncritical for personal use; review before redistribution or commercial use. |

## Conventions

- Twelve-factor style: configuration and secrets only via environment variables or secret files, never in images.
- Stateless services, structured logs on stdout, health/readiness/liveness endpoints, graceful shutdown.
- Database migrations (Alembic) run as a separate step before start and stay backward compatible.
- Contracts only evolve backward compatibly; events are never reinterpreted, new versions are added instead.
