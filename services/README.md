# services

Deployable MowDeck services, one uv workspace package per service (AGPL-3.0-only). Every service is generated from the service template (`uv run copier copy --trust templates/service services/<name>`, see `docs/dev/service-template.md`) and ships its own `README.md`, `pyproject.toml`, `Dockerfile` and `compose.snippet.yml`; Kubernetes deployments use the umbrella chart `infra/helm/mowdeck`.

| Service | Status | Purpose |
| --- | --- | --- |
| [`assistant`](assistant) | 0.1.0 | Optional (Compose profile `assistant`): chat control, a typed request becomes a work order of mower actions the user confirms in the web app; HTTP only with the caller's token, executes nothing itself (ADR 0075) |
| [`camera-relay`](camera-relay) | 0.1.0 | Optional (Compose profile `relay`): shared camera sessions per account and mower, leases with a grace period, signed tickets and lifecycle events (ADR 0045); answers `unavailable` until the media plane and the source drivers follow |
| [`matter-server`](matter-server) | 0.1.0 | Optional (Compose profile `matter`): every mower as its own Matter node (Robotic Vacuum Cleaner) for Apple Home, Google Home and Home Assistant, pairing codes for the web app (ADR 0063). TypeScript (matter.js on Node 24), a pnpm package instead of a uv one |
| [`billing`](billing) | 0.1.0 | Optional (Compose profile `billing`): subscriptions with Stripe Checkout and the customer portal, signed webhooks processed once that set plan statuses in Entitlements |
| [`device-gateway`](device-gateway) | 0.1.0 | Mower connections (simulated in Phase 1), commands, telemetry, connection events |
| [`entitlements`](entitlements) | 0.1.0 | Plans, limits and feature gates: answers whether an account may use a feature (`entitlements.check`); Billing sets plans with `entitlements.plan.set` |
| [`fleet`](fleet) | 0.1.0 | Sites, groups, mowers, the Device-Gateway's registry, commands checked per command kind and command records |
| [`identity`](identity) | 0.1.0 | Accounts, users and sub-users, roles, sessions, API tokens, audit log; issues the access tokens and writes the authorization tuples of its subjects |
| [`planner`](planner) | 0.1.0 | Mowing patterns as plug-ins: computes, validates and previews spot mowing routes, coverage and gaps from the position track, pattern templates |
| [`notifications`](notifications) | 0.1.0 | Notifications from the mower states (finished, stuck, battery low, offline) to subscribers by Web Push, ntfy and channel plug-ins; inbox |
| [`scheduler`](scheduler) | 0.1.0 | Mowing schedules (weekly, every N days, once) for mowers and groups with a rain pause; starts mowers through Fleet on behalf of the schedule owner |
| [`realtime`](realtime) | 0.1.0 | Live mower states and command results over WebSocket, checked per mower with `can_view` |
| [`vision`](vision) | 0.1.0 | AI assist for stuck mowers and Find Dock: a vision model (any family through LiteLLM, per-user providers or the installation's) proposes a maneuver, a user approves it, the service drives it through Fleet's drive sessions with blades off |
| [`telemetry`](telemetry) | 0.1.0 | Latest state, history, position track and command timeline of every mower |
