# Architecture decisions

Every architecture decision is recorded as an ADR in a MADR-like format with the headings Status, Context, Decision and Consequences. Files are named `NNNN-kebab-case-title.md`; start from `template.md` in this directory. Decisions are never rewritten: a changed decision gets a new ADR that supersedes the old one. A test in `tools/repo-checks` enforces the structure.

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-openfga-for-authorization.md) | OpenFGA instead of self-built authorization | Accepted |
| [0002](0002-self-built-identity-service.md) | Self-built Identity service in FastAPI instead of Keycloak or Zitadel | Accepted |
| [0003](0003-sveltekit-for-the-web-app.md) | SvelteKit for the web app | Accepted |
| [0004](0004-monorepo-with-uv-workspace.md) | Monorepo with a uv workspace | Accepted |
| [0005](0005-licensing.md) | AGPL-3.0 for services and apps, Apache-2.0 for libraries | Accepted |
| [0006](0006-postgresql-for-all-persistence.md) | PostgreSQL for all persistence, no SQLite | Accepted |
| [0007](0007-nats-jetstream-event-bus.md) | NATS JetStream as event bus | Accepted |
| [0008](0008-traefik-as-gateway.md) | Traefik as gateway | Accepted |
| [0009](0009-command-and-event-envelopes.md) | Command and event envelopes, subject naming and de-duplication | Accepted |
| [0010](0010-python-3-13.md) | Python 3.13 for all services and libraries | Accepted |
| [0011](0011-gpl-package-for-pymammotion-adapter.md) | Separate GPL-3.0 package for the PyMammotion adapter | Accepted |
| [0012](0012-mower-control-interface-and-safety-rules.md) | Mower control interface and safety rules | Accepted |
| [0013](0013-device-gateway-registry-and-sharding.md) | Device-Gateway registry, sharding and per-mower concurrency | Accepted |
| [0014](0014-authorization-model-and-tuple-ownership.md) | Authorization model, tuple ownership and the mowdeck-authz client | Accepted |
| [0015](0015-tokens-sessions-and-key-rotation.md) | Tokens, sessions and key rotation | Accepted |
| [0016](0016-fleet-commands-and-authorization-per-command-kind.md) | Fleet commands and authorization per command kind | Accepted |
| [0017](0017-telemetry-storage-and-retention.md) | Telemetry storage and retention | Accepted |
| [0018](0018-realtime-websocket-protocol.md) | Realtime WebSocket protocol | Accepted |
| [0019](0019-mammotion-adapter-transports.md) | Mammotion adapter transports, manual drive and secrets | Accepted |
| [0020](0020-web-app-architecture.md) | Web app architecture | Accepted |
| [0021](0021-browser-auth-flow.md) | Browser authentication flow | Accepted |
| [0022](0022-notifications-rules-and-delivery.md) | Notifications: rules, recipients and delivery | Accepted |
| [0023](0023-map-rendering-and-projection.md) | Map rendering and projection | Accepted |
| [0024](0024-scheduler-recurrence-rain-pause-and-command-issuing.md) | Scheduler: recurrence, rain pause and command issuing | Accepted |
| [0025](0025-home-assistant-integration-as-api-client.md) | Home Assistant integration as a pure API client | Accepted |
| [0026](0026-manual-drive-channel.md) | Manual drive channel over the Realtime WebSocket | Accepted |
| [0027](0027-health-history-and-theft-alarm.md) | Health history and theft alarm | Accepted |
| [0028](0028-entitlements-plans-and-enforcement.md) | Entitlements: plans as data and enforcement | Accepted |
| [0029](0029-helm-chart-and-release-pipeline.md) | Helm chart and release pipeline | Accepted |
| [0030](0030-mowing-patterns-and-planner.md) | Mowing patterns as plug-ins and the Planner | Accepted |
| [0031](0031-billing-with-stripe-adapter.md) | Billing with a Stripe adapter | Accepted |
| [0032](0032-live-camera-over-agora.md) | Live camera over Agora, joined directly by the browser | Accepted |
| [0033](0033-vision-assisted-recovery-with-approval.md) | Vision-assisted recovery with human approval | Accepted |
| [0034](0034-linked-cloud-accounts-and-device-discovery.md) | Linked cloud accounts and device discovery | Accepted |
| [0035](0035-server-side-camera-frames-and-automatic-analysis.md) | Server-side camera frames and automatic AI analysis on errors | Accepted |
| [0036](0036-native-app-shells-with-capacitor.md) | Native app shells with Capacitor, loading the web app from the server | Accepted |
| [0037](0037-camera-panorama-calibration.md) | Camera panorama calibration in the browser, with the AI supplying points only | Superseded |
| [0038](0038-dual-link-per-mower-and-firmware-updates.md) | Dual link per mower (cloud plus Bluetooth bridge) and firmware updates | Accepted |
| [0039](0039-spot-mowing-by-mowdeck-driven-paths.md) | Spot mowing by MowDeck-driven paths | Accepted |
| [0040](0040-link-heartbeat-for-online-status.md) | Link heartbeat for online status | Accepted |
| [0041](0041-spot-runs-from-the-dock-undock-transit-return.md) | Spot runs from the dock: undock, transit and return | Accepted |
| [0042](0042-drive-calibration.md) | Drive calibration | Accepted |
| [0043](0043-turn-styles.md) | Turn styles | Accepted |
| [0044](0044-drive-view-with-camera.md) | Drive view with camera | Accepted |
| [0045](0045-camera-relay.md) | Camera relay | Accepted |
| [0046](0046-device-settings-as-capabilities.md) | Device settings as capabilities | Accepted |
| [0047](0047-rtk-base-station-as-a-fleet-device.md) | The RTK base station as a Fleet device | Accepted |
| [0048](0048-maintenance-commands.md) | Maintenance commands | Accepted |
| [0049](0049-manual-driving-over-the-mammotion-cloud.md) | Manual driving over the Mammotion cloud | Accepted |
