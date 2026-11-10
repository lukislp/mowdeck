# ADR 0082: Deleting your own account inside the app

## Status

Proposed (2026-11-10). Draft: the investigation below shows that real deletion needs more than one pull request, so this ADR records the findings, the proposed design and the split. Nothing in the web app calls the deletion yet. Builds on [ADR 0057](0057-step-up-reauthentication.md) (re-authentication), [ADR 0076](0076-demo-sandboxes.md) (the purge calls of the demo sandboxes) and [ADR 0074](0074-nats-authentication-and-subject-permissions.md) (NATS permissions).

## Context

App Store guideline 5.1.1(v): an app that lets people create an account must let them start deleting it inside the app. Deactivation is not enough and the user's content has to go. MowDeck offers registration (`apps/web/src/routes/register`, linked from the sign-in page), so the iOS app needs an in-app path, and the same path is what the GDPR right to erasure needs on any installation.

### What exists today

**Identity's model.** Every user belongs to exactly one account: a registration creates the account, its owner user and a membership with `is_owner`; sub-users are created by an account admin and belong to that account (`memberships.add` appears only in the registration and in the creation of a sub-user). Owner and admin are fixed relations on the membership, the other rights are roles. Usernames and e-mail addresses are unique installation wide.

**Account deletion.** `DELETE /api/v1/identity/accounts/{account_id}` (permission `ManageAccount`, which means the owner) calls `AccountService.request_deletion`: the caller must send the password or, without it, have a recent step-up (ADR 0057); API tokens and service identities cannot (403 `step-up-unavailable`). It sets the account to `deleting`, `delete_after = now + IDENTITY_DELETION_GRACE_DAYS` (default 7), revokes the API tokens, ends the sessions of the other members, audits `account.deletion_requested` and answers 202. `POST .../restore` undoes it. **Nothing ever purges the account**: the docstring says "the purge after the grace period is a later job", and no code reads `delete_after`. So today the endpoint locks an account for good and deletes no data. The web app does not call it. The only complete cleanup of an account is the demo sandbox purge (ADR 0076).

**Removing a user.** `DELETE .../users/{user_id}` (`ManageUsers` plus step-up) deletes a sub-user (user row, sessions, passkeys and external identities by cascade) or removes a person's membership, audits `user.deleted` or `user.removed` and publishes `identity.user.deleted`. It refuses the owner and **refuses yourself** ("you cannot do this to yourself"), so a member has no way to remove themselves, and an owner cannot be removed at all. A person's user row is not deleted when the membership goes (only owned sub-users are), so a removed person's username, display name, e-mail and password hash stay in `users`.

**How other services learn about it.** `identity.user.deleted` is consumed by Notifications and Vision (durable consumers `notifications-identity` and `vision-identity`, both also on `identity.user.disabled`), which drop the user's own rows. Nobody consumes anything for a deleted account: Identity has no `identity.account.deleted` event. Fleet, Scheduler, Telemetry, Planner, Entitlements and Billing do not react to a deleted user or account. Seven services do have an idempotent "delete every row of this account" endpoint, `DELETE /api/v1/<service>/demo/accounts/{id}`, but it is the demo purge: it needs the demo seed secret and answers 404 outside demo mode.

### Data map: who holds what, and what removes it today

| Owner | Data | Removed today by |
| --- | --- | --- |
| Identity | users (name, display name, e-mail, password hash), memberships, roles, role assignments, sessions, API tokens, passkeys, external identities (GitHub, Apple), app login requests and codes, known devices, onboarding progress, login attempts, audit log, OpenFGA tuples | Sub-user removal removes that user's rows (not the audit rows, not a person's user row). The demo purge removes a sandbox account's rows. For a real account nothing |
| Fleet | sites, groups, mowers with their groups, links and area names, linked vendor accounts with their encrypted credentials, base stations, commands and drive sessions (retention 90 days), audit log (actor subject and kind), OpenFGA tuples of mowers, sites and groups | Demo purge (including the tuples). Mower and linked account have their own delete endpoints. The `created_by` columns stay |
| Telemetry | latest state, history, positions and thinning, snapshots, health and daily health, blade changes, command timeline, theft tracking sessions, links and photos, virtual zones, presence and days | Demo purge. Retention jobs for old rows ([telemetry retention](../ops/telemetry-retention.md)) |
| Scheduler | schedules, runs, mower status, places, capabilities, mow angles, rotation | Demo purge |
| Notifications | preferences, web push subscriptions, APNs devices, ntfy target, inbox, notification images, deliveries, geofences, alarm silences, live activity devices and activities, mower watch | `identity.user.deleted` for the user's rows. Demo purge for an account |
| Vision | proposals with steps and images, provider requests, per-user provider keys, preferences, auto watch | `identity.user.deleted` for the user's rows. Demo purge for an account |
| Planner | mower facts, templates | Demo purge |
| Entitlements | account plan, plan history | Demo purge |
| Billing | accounts, Stripe customers, subscriptions, invoice references, webhook events | Nothing. `BillingService.cancel(account_id)` (cancels every open subscription at the provider, documented "account deletion") exists and has no caller |
| Assistant | nothing (owns no data, never logs the text) | not needed |
| Device-Gateway, Realtime, Camera-Relay | no database tables; session and simulator state in memory, stopped by `mower.removed` | not needed |
| Third parties | APNs registrations (Apple refuses a push to a removed token), Stripe customer and subscription, vendor clouds (Mammotion, Husqvarna, Worx), the optional cloud AI provider | APNs: stops by itself. The rest: nothing |
| Backups | `pg_dumpall` before updates into `MOWDECK_BACKUP_DIR` ([Compose](../ops/compose.md)) | The documents state no retention |

### Why the first pull request stops here

A button on the strength of the existing endpoint would be dishonest: it only locks the account. Making it delete needs a purge job, a generalised and authenticated version of the demo purge that seven services answer, a new way for a member to remove themselves, a scrub of personal data in the audit log, the provider call to Stripe and a decision on the audit rules. That is several services with their own tests plus an Identity job with retries: not one reviewable pull request, and a UI that deleted only part of it must not ship. So this ADR is the deliverable of the first step.

## Decision

Proposed; every point marked "to confirm" needs the maintainer's answer before the follow-up pull requests start.

### 1. Two operations, one mechanism

- **Delete the account** (owner): the whole account and everything of it in every service.
- **Delete me** (a member without the owner relation, sub-users included): only the user, their membership and their personal rows.

An owner of an account that still has other members cannot delete only themselves (to confirm): the app offers "Delete account", states the number of members who lose access with it, and explains that removing the members first (Settings -> Users) is the alternative. There is no ownership hand-over in the product (`promote_owner` is an operator command), so a hand-over is not part of this decision. Everyone else sees "Delete me".

### 2. Re-authentication and who may call

Both operations need the step-up of ADR 0057 (`require_recent_auth`: password or passkey, in the native app the password) and a **login session**: API tokens and service identities get 403 `step-up-unavailable`, no scope waives it. The web flow reuses the existing `StepUp` sheet, then an explanation of what is deleted and that it cannot be undone, and a typed confirmation (the username). The existing `DELETE /accounts/{id}` keeps its optional `password` field for compatibility; a new `DELETE /api/v1/identity/me` (login session plus step-up) applies the policy by role, so the client does not have to know which operation applies.

### 3. Grace period and the purge

Keep the existing grace period for the account (to confirm): it protects against a stolen session or a mistake, `restore` exists, and the deletion is not a deactivation because the purge follows without anyone's action. The user-level deletion has no grace period. The in-app text says when the data goes. `IDENTITY_DELETION_GRACE_DAYS=0` purges at once. There is deliberately no switch to turn deletion off.

An Identity background job (the place that already runs the sandbox sweep) finds accounts whose `delete_after` has passed and purges them in the order Fleet first (removing the mowers stops their simulators), then the other services, then Identity's own rows and tuples, as the sandbox purge of ADR 0076 does, and records in a registry table which services confirmed (as `demo_sandboxes.purged` does), so a retry skips them. Retries use backoff; a service that stays down keeps the account in `purging` and logs an error.

### 4. How the services delete: explicit calls, not events

The argument of ADR 0076 holds: an event gives Identity no completion and no visible retry, and a deleted account must be provably deleted everywhere. The seven existing purge endpoints become the mechanism for real accounts by replacing the demo-only guard with an internal one. Two options:

- **A (recommended, to confirm): core NATS request and reply** `account.erase.<service>` (request `{account_id}`, reply `{deleted: n}`), the pattern of `entitlements.plan.set` and `fleet.commands.issue`. Services answer, Identity requests; the subjects go into `tools/repo-checks/scripts/nats_auth.py`, which generates the permissions of ADR 0074. No new secret and no HTTP route a misconfigured gateway could expose.
- **B: the HTTP routes** under `/internal/accounts/{id}`, authorised by a new shared internal token, hidden from the API documents and stripped by Traefik like the demo seed header. It reuses the demo routes one to one but adds a secret to Compose and Helm and a route whose only protection is a header.

Either way the application-layer purge classes of the services (Fleet's `AccountPurge`, and the Scheduler's, Planner's, Notifications', Vision's, Telemetry's and Entitlements' equivalents) are reused: they are the domain operation, only the adapter that triggers it changes, and the demo route keeps calling the same class. Billing gets its own. Every handler is idempotent and answers with a count.

For the user-level deletion the existing event `identity.user.deleted` stays the mechanism (Notifications and Vision already consume it); Fleet, Telemetry and Planner get consumers only for what holds personal data beyond an opaque id (for example Vision's `requested_by` snapshot).

### 5. Third parties

- **Stripe:** before Identity purges, Billing cancels the open subscriptions (`BillingService.cancel`, already written) and, to confirm, deletes the personal data of the Stripe customer that it controls; invoices the operator must keep for tax law stay at Stripe and in Billing's invoice references until the legal period ends, and the privacy text names them.
- **APNs:** the app removes its registration on sign-out (`nativeShell.signingOut()`), and the purge deletes the rows, so no push goes to the device again.
- **Vendor clouds** (Mammotion, Husqvarna, Worx): MowDeck removes the stored credentials with the linked account and cannot delete what the vendor holds. The in-app text and the privacy text say so.
- **Cloud AI provider** (ADR 0081, in review): a request already sent stays with the provider under its terms; MowDeck deletes the pictures and keys it stores.
- **Backups:** deleted data stays in the operator's backups until they expire. The operator documentation must state a retention and the privacy template names it (the documents state none today; to confirm what the shipped procedures keep).

### 6. Audit log

The audit logs are append-only on purpose (database triggers; only the demo purge may delete, through `mowdeck.audit_purge`). A deletion keeps its audit row (`account.deleted`, `user.deleted`: ids, time, actor id, counts) because that is the proof the request was honoured. Personal data in the rows (username, display name or e-mail in `details`, usernames of registration rows) is scrubbed at deletion by a narrow update path guarded the same way, replacing them with a fixed marker; ids stay as pseudonymous references. To confirm: that this satisfies the repository's audit rule (a row survives, its personal fields do not). Opaque `created_by` ids in other services stay: once Identity's user row is gone they identify nobody.

### 7. Web app

Settings -> Profile gets a separate danger area under the sign-out ("Delete account" or "Delete me" by role, the explanation, the member count, a blocked state with its reason, `DemoLock` in the demo). The flow is a lib service (no rules in the component): load what applies, step-up through the existing controller, call the endpoint, then the same clean exit as sign-out (`nativeShell.signingOut()` first so push tokens and widget credentials go, then the session logout). Component and unit tests cover pending, error, blocked and demo-locked.

## Consequences

- Nothing in the web app changes with this ADR. The App Store requirement stays open until the follow-ups ship; submitting earlier would fail review.
- Real deletion needs the generalised purge in seven services, a new one in Billing and in Identity, the Identity job, `DELETE /identity/me`, the audit scrub, the web flow, the privacy text, backup retention in the operator documentation and an end-to-end test that registers, creates data in every service, deletes and checks every table.
- A new service that stores per-account data must answer `account.erase.<service>` (this extends the checklist of ADR 0076, which covers the demo only); a repository test should fail for a service with tables and no handler.
- The grace period means an account's data lives up to 7 days after the request; the text must say so.

### Proposed split

1. **Contract and Identity core:** request and reply models in `mowdeck-contracts`, the registry table and the purge job in Identity (expiry of the grace period, order, retries, audit), Identity's own rows and tuples, the scrub of the audit rows; the services' answers faked. Tests at the application layer.
2. **Service handlers:** one pull request for Fleet, Scheduler, Telemetry, Planner and Entitlements (reuse of the purge classes behind the new adapter, `nats_auth.py`), one for Notifications and Vision, one for Billing (cancel at Stripe, then delete). Plus a repository test that every service with tables has a handler.
3. **Member self-removal:** `DELETE /identity/me`, the role policy (owner with members blocked), the user-level cleanup in Fleet, Telemetry and Vision, tests across accounts.
4. **Web app, docs and e2e:** the Settings flow with its states, the privacy template and the operator documentation (retention, backups), the Playwright spec, the README.

Steps 1 to 3 can be reviewed independently. Step 4 is the only one users see and comes last, because it makes the promise.

### Decisions to confirm

1. Owner of an account with other members: no self-deletion of the person alone, "Delete account" removes everyone with it (this ADR), or require the members to be removed first.
2. Keep the 7-day grace period for accounts (this ADR) or purge at once.
3. Core NATS `account.erase.<service>` (A) or HTTP internal routes with a token (B).
4. Scrub the personal fields of audit rows and keep the rows (this ADR).
5. What the deletion of the Stripe customer covers and which invoice references Billing keeps.
6. The backup retention the shipped procedures promise.
