# ADR 0028: Entitlements: plans as data and enforcement

## Status

Accepted (2026-10-10)

## Context

Phase 4 ("Flotte & Abo") separates money from permission: a Billing service handles payment, an Entitlements service answers "may account X use feature Y?" (project plan, "Abo & Zahlung", "Dienste im Überblick"). With `billing.enabled = false` (the default of a home installation) Billing is not started and every account has the plan "Unlimited"; no other service may need to know. Plans are data with limits (mowers, users) and features (spot mowing, camera, AI), a trial in days, and a grace period on payment problems after which only viewing and the emergency stop remain, "never a mower that stops in the middle of the garden because of a subscription". Every function sits behind a feature key that modules declare in their descriptors ("Erweiterbarkeit"). Operator admins manage accounts and plans but see no mower data ("Konten"); the authorization model already has `platform:main` with `can_manage_accounts` and `can_manage_plans` ([ADR 0014](0014-authorization-model-and-tuple-ownership.md)). Billing (the Stripe adapter) is deferred; this decision covers Entitlements and its enforcement.

## Decision

**Plans are data, seeded from a file.** `infra/entitlements/plans.yaml` lists the plans (`key`, `name`, `limits`, `features`, `trial_days`, `is_default`, `active`); the service upserts them by key at every start into its own `plans` table. A plan is rewritten only when its file entry changed since the last start (a SHA-256 `file_digest`), so an operator's edit through `PUT /plans/{key}` survives restarts until the file entry itself changes. Plans are never deleted; `active: false` stops offering one. The shipped plans: `unlimited` (no limits, `*`), `home` (4 mowers, 5 users, 1 site, 2 groups, 10 schedules, 90 days history; the core features plus schedules, notifications, manual drive, Home Assistant, map, health; 30 trial days) and `fleet` (no limits; additionally `fleet.bulk_commands`, `reports`, `api_tokens.scoped`; 14 trial days). Limits are `max_mowers`, `max_users`, `max_sites`, `max_groups`, `max_schedules`, `history_days`; an omitted limit is unlimited and an unknown limit name is rejected, so a typo never means "unlimited".

**Feature keys are hierarchical; the catalogue comes from module descriptors.** `notifications` grants `notifications.push`; `*` grants everything. Entitlements collects the `features` of the descriptors at `MOWDECK_ENTITLEMENTS_MODULE_URLS` (like Identity's permission catalogue) into `features` and serves `GET /features`. Entitlements itself declares the core keys `view`, `control` and `emergency_stop`; Fleet declares `fleet.bulk_commands`, Realtime `manual_drive`, Scheduler `schedules`, Notifications `notifications` and `notifications.push`. Plan features that no module declares are kept and only logged (`unknown_features` in `GET /plans/{key}`), so a plan file may lead a deployment ("reports" before the Reports service exists).

**Resolution.** `account_plans` holds the assignment of an account (`plan_key`, `status` `active|trial|grace|expired`, `since`, `trial_until`, `grace_until`, `source` `default|manual|billing`); `plan_history` keeps every assignment with reason and actor. `identity.account.created` gives a new account the default plan (`MOWDECK_ENTITLEMENTS_DEFAULT_PLAN`, default `home`), as a trial when the plan has trial days; rows are written whether billing is enabled or not, so enabling billing later finds every account. The kit setting `MOWDECK_ENTITLEMENTS_BILLING_ENABLED` decides:

- `false` (default): every account resolves to `unlimited`, `active`, whatever is assigned; the API says so (`billing_enabled: false`).
- `true`: the account's row applies; an account without a row gets the default plan (`active`); an unknown default plan falls back to the plan marked `is_default`, and failing that to a plan that allows nothing new (a broken file never opens everything up).

Status transitions are computed, never written by time: a trial whose `trial_until` has passed and a grace period whose `grace_until` has passed count as `expired`.

**Grace and expired: only view and emergency stop.** In `grace` and `expired` the features shrink to `view` and `emergency_stop`, and every limit check is refused (nothing new may be created; the numeric limits no longer matter). Existing mowers, sites, groups and schedules are never removed or stopped: no plan change ever sends a command. `view` and `emergency_stop` are never refused by any plan, any status or any outage, and clients do not even ask for them; Fleet checks `control` for every command except the emergency stop, so a grace account can always stop a mower.

**One question over the bus, one snapshot cached per account.** Services ask over core NATS (`entitlements.check`, `EntitlementCheckRequest{account_id, feature | limit: {key, current}}` -> `EntitlementCheckReply{allowed, plan, status, reason, limit, current, snapshot}`, queue group `entitlements`). The reply carries the account's `EntitlementSnapshot` (plan, status, limits, features); `EntitlementsClient` in `mowdeck-service-kit` caches it for 60 s (`MOWDECK_ENTITLEMENTS_CACHE_SECONDS`) and decides later questions locally with `EntitlementSnapshot.decide`, the same function the service uses. `entitlements.plan.changed` (`PlanChanged`, on manual assignment for one account; on plan edits and at every Entitlements start for all accounts) drops cached snapshots in every instance. Each enforcement point is one call (`require_limit`, `require_feature`).

**When Entitlements does not answer, the local billing switch decides.** The client returns `allowed=True` (plan `unlimited`) when the calling service's own `MOWDECK_ENTITLEMENTS_BILLING_ENABLED` is `false`, and `allowed=False` (`reason=entitlements_unavailable`) when it is `true`. Failing open for features and closed for limits would be inconsistent; this rule is the same for both and follows the installation switch: a home installation keeps working without Entitlements, an installation that sells plans refuses growth while it cannot check. Outages are never cached. `view` and `emergency_stop` stay allowed either way.

**Enforcement points.**

| Service | Check | Refusal |
| --- | --- | --- |
| Fleet | `max_mowers` on registration, `max_sites` on site creation, `max_groups` on group creation | 409 `urn:mowdeck:problem:plan-limit`, `errors` = `{parameter: <key>, code: "limit", detail: "<limit>"}` and `{..., code: "current", ...}` |
| Fleet | `control` for every command except `emergency_stop`; additionally `fleet.bulk_commands` for bulk commands | 403 `urn:mowdeck:problem:feature-not-in-plan`; `fleet.commands.issue` answers the new code `not_in_plan` (the Scheduler records a failed run, it does not count as a permission skip) |
| Scheduler | `schedules` and `max_schedules` on create | 403 / 409 as above |
| Identity | `max_users` on sub-user creation; the `plan` claim at token issue (`plan_of` through the client) | 409; with billing disabled the claim is `unlimited`, with billing enabled and no answer it is empty |
| Realtime | `manual_drive` on `drive_start` | `drive_not_allowed`, Fleet is not asked |
| Notifications | `notifications.push` on `POST /push-subscriptions` | 403 |

Existing hard ceilings (`MOWDECK_MAX_MOWERS_PER_ACCOUNT`, `MOWDECK_SCHEDULER_MAX_SCHEDULES_PER_ACCOUNT`) stay as installation safety limits below any plan.

**API and operators.** `/api/v1/entitlements/`: `GET /me` (the caller's account: plan, status, source, limits, features, trial and grace ends, `billing_enabled`), `GET /features`, `GET /plans` (public, the offered plans), and for operator admins (`can_manage_plans` on `platform:main`): `GET/PUT /plans/{key}`, `GET /accounts` (paged, assignment and effective plan, no mower data), `GET /accounts/{id}` (with history), `PUT /accounts/{id}/plan` (manual assignment with a reason; history row, `entitlements.plan.changed`). Identity gains `GET /api/v1/identity/platform/accounts` (`can_manage_accounts`; id, name, kind, status, created, owner username) and the CLI command `grant-platform-admin USERNAME [--revoke]`, which writes `platform:main#operator_admin`. Usage counters are not part of `/me`: the counts live in other services' databases and the clients know them.

## Consequences

- A home installation behaves as before: billing is off, every check is allowed, and the only cost is one cached request per account and minute (or a fast "no responders" while Entitlements is not deployed).
- Enabling billing is one switch per installation plus a restart of Entitlements; Entitlements announces `entitlements.plan.changed` for all accounts at start, so no service keeps a stale "unlimited" for a minute.
- A plan change takes effect within one request in the services (event) and with the next access token in the `plan` claim; the claim is informational, services decide through the client.
- An Entitlements outage with billing enabled refuses growth and gated features until it answers; viewing and stopping keep working.
- Operator admins live only in OpenFGA (`platform:main#operator_admin`), not in Identity's database; a rebuilt OpenFGA store needs the CLI grant again.
- Not covered yet: Billing and its `PaymentProvider` (status `grace`/`expired` are set by hand until then), usage counters in `/me`, plan-aware UI in the web app, the `history_days` limit (declared, enforced by Telemetry later), features without an enforcing module (`home_assistant`, `map`, `health`, `reports`, `api_tokens.scoped`).
