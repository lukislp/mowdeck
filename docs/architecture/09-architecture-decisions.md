# 9. Architecture decisions

All decisions are recorded as ADRs in the [ADR index](../adr/README.md).

| ADR | Decision |
| --- | --- |
| [0001](../adr/0001-openfga-for-authorization.md) | OpenFGA instead of self-built authorization |
| [0002](../adr/0002-self-built-identity-service.md) | Self-built Identity service in FastAPI |
| [0003](../adr/0003-sveltekit-for-the-web-app.md) | SvelteKit for the web app |
| [0004](../adr/0004-monorepo-with-uv-workspace.md) | Monorepo with a uv workspace |
| [0005](../adr/0005-licensing.md) | AGPL-3.0 and Apache-2.0 licensing |
| [0006](../adr/0006-postgresql-for-all-persistence.md) | PostgreSQL for all persistence |
| [0007](../adr/0007-nats-jetstream-event-bus.md) | NATS JetStream as event bus |
| [0008](../adr/0008-traefik-as-gateway.md) | Traefik as gateway |
| [0009](../adr/0009-command-and-event-envelopes.md) | Command and event envelopes, subject naming and de-duplication |
| [0010](../adr/0010-python-3-13.md) | Python 3.13 for all services and libraries |
| [0011](../adr/0011-gpl-package-for-pymammotion-adapter.md) | Separate GPL-3.0 package for the PyMammotion adapter |
| [0012](../adr/0012-mower-control-interface-and-safety-rules.md) | Mower control interface and safety rules |
| [0013](../adr/0013-device-gateway-registry-and-sharding.md) | Device-Gateway registry, sharding and per-mower concurrency |
| [0014](../adr/0014-authorization-model-and-tuple-ownership.md) | Authorization model, tuple ownership and the mowdeck-authz client |
| [0015](../adr/0015-tokens-sessions-and-key-rotation.md) | Tokens, sessions and key rotation |
| [0016](../adr/0016-fleet-commands-and-authorization-per-command-kind.md) | Fleet commands and authorization per command kind |
| [0017](../adr/0017-telemetry-storage-and-retention.md) | Telemetry storage and retention |
| [0018](../adr/0018-realtime-websocket-protocol.md) | Realtime WebSocket protocol |
| [0019](../adr/0019-mammotion-adapter-transports.md) | Mammotion adapter: cloud first, BLE via ESPHome proxy, manual drive only over BLE, secret references, exact pins |
| [0020](../adr/0020-web-app-architecture.md) | Web app architecture |
| [0021](../adr/0021-browser-auth-flow.md) | Browser authentication flow |
| [0022](../adr/0022-notifications-rules-and-delivery.md) | Notifications: rules derived from telemetry in the service, subscribers re-checked with `can_view`, outbox with `SKIP LOCKED`, channel plug-ins |
| [0023](../adr/0023-map-rendering-and-projection.md) | Map rendering and projection: Leaflet with a metric grid in local metres (`CRS.Simple`) by default, WGS84 only with configured tiles, conversion in the browser checked against golden vectors, Geoman deferred |
| [0024](../adr/0024-scheduler-recurrence-rain-pause-and-command-issuing.md) | Scheduler: recurrence on `zoneinfo`, firing through Fleet's `fleet.commands.issue` on behalf of the owner with a re-check of `can_control`, `SKIP LOCKED` ticker, Open-Meteo rain pause behind a weather port |
| [0025](../adr/0025-home-assistant-integration-as-api-client.md) | Home Assistant integration: pure API client without shared Python code, API token exchange, push over the Realtime WebSocket with polling fallback, dedicated HACS repository published from the monorepo |
| [0026](../adr/0026-manual-drive-channel.md) | Manual driving: session opened once over the Realtime WebSocket and recorded by Fleet (`fleet.drive.start`/`stop`, `can_drive`, one open session per mower), frames published by Realtime as `ManualDrive` commands (rate limited, `ttl_ms` 1000), the Device-Gateway controller stays the only safety authority |
| [0027](../adr/0027-health-history-and-theft-alarm.md) | Health history computed incrementally in Telemetry's state consumer (UTC days, blade hours heuristic, blade reset, `telemetry.blade_hours.threshold`), theft alarms in Notifications (lift, unexpected dock leave, geofence exit; critical, bypass quiet hours, acknowledge silences an hour) |
| [0028](../adr/0028-entitlements-plans-and-enforcement.md) | Plans as data seeded from `infra/entitlements/plans.yaml`, `MOWDECK_ENTITLEMENTS_BILLING_ENABLED` (off: every account `unlimited`), hierarchical feature keys from module descriptors, `entitlements.check` with a per-account snapshot cache in the kit client (no answer: the local billing switch decides), grace and expired keep only view and emergency stop, operator endpoints on `platform:main` |
| [0029](../adr/0029-helm-chart-and-release-pipeline.md) | Helm chart and release pipeline: one umbrella chart with a template per service and the Compose settings under the same names, NATS and OpenFGA as optional official subcharts, PostgreSQL through CloudNativePG (one database and role per service) or external, migrations as hooks (external database) or per-revision Jobs (in-cluster) with pods waiting for their schema and model, Ingress resources instead of Traefik CRDs, multi-arch images through QEMU and the chart as an OCI artifact in GHCR from tags only |
| [0030](../adr/0030-mowing-patterns-and-planner.md) | Mowing patterns as plug-ins in the pure library `mowdeck-patterns` (Shapely, entry point group `mowdeck.patterns`, settings as JSON Schema), one validation chain for every plan, chains and templates, plans inline in `StartMow.spot_plan` (encoded segments, SHA-256 hash, optional native job), execution by `capabilities.custom_paths` or the native job, Fleet gates `spot_mowing` and records the hash, coverage from Telemetry's track over `tracks.positions.query`, the Planner owns only templates and a snapshot directory |
| [0031](../adr/0031-billing-with-stripe-adapter.md) | Billing as an optional service (Compose profile `billing`, Helm `services.billing.enabled`, not started when disabled; the web app probes `GET /billing/status`), `PaymentProvider` with a Stripe adapter (Checkout and customer portal, one customer per account, prices as data in `infra/billing/prices.yaml`) and a fake, webhooks verified by signature and processed once (`webhook_events` ledger, stale events ignored), Stripe statuses mapped to plan statuses set in Entitlements over `entitlements.plan.set` (`source=billing`), grace from the end of the last paid period, owner only (`can_manage_billing`) |
| [0032](../adr/0032-live-camera-over-agora.md) | Live camera over Agora: the browser joins the Mammotion camera channel directly with the Agora Web SDK (no media through MowDeck); the adapter fetches the stream token, the Device-Gateway answers `gateway.mower.<id>.camera.session`, Fleet checks `can_view` and the plan feature `camera` and audits; the Agora domains enter the CSP only with `MOWDECK_WEB_CAMERA_ENABLED=true` |
| [0033](../adr/0033-vision-assisted-recovery-with-approval.md) | Vision-assisted recovery: a vision model (any family through LiteLLM; per-user providers with encrypted keys, an optional installation model behind `ai_recover_hosted`) proposes at most six drive steps, a user with `can_drive` approves, the service drives them through `fleet.drive.*` on the approver's behalf (`origin: vision`) at 5 Hz with `ttl_ms` 1000 and blades off, aborts on emergency stop, sensor stop, geofence, missing state or cancellation, verifies, at most three rounds; `drive_recoverable` errors are the only ones the controller lets drive frames through |
| [0034](../adr/0034-linked-cloud-accounts-and-device-discovery.md) | Linked cloud accounts: Fleet stores Mammotion accounts per MowDeck account (password AES-256-GCM under `MOWDECK_CREDENTIAL_ENCRYPTION_KEY`), the Device-Gateway logs in and lists the devices on `gateway.cloud.discover`, cloud mowers reference a linked account and its device, one PyMammotion session per account shared by its mowers and the discovery, one physical mower (IoT id) in one MowDeck account installation-wide (409 `mower-already-registered`), old cloud mowers moved on start |
