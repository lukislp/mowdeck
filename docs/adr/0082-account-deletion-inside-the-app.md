# ADR 0082: Deleting your own account inside the app

## Status

Accepted (2026-11-10). The investigation below showed that real deletion needs several pull requests; the maintainer decided the open points the same day. Step 1 (contract and Identity core) is implemented with this ADR, the services' handlers and the web flow follow. Nothing in the web app calls the deletion yet. Builds on [ADR 0057](0057-step-up-reauthentication.md) (re-authentication), [ADR 0076](0076-demo-sandboxes.md) (the purge calls of the demo sandboxes) and [ADR 0074](0074-nats-authentication-and-subject-permissions.md) (NATS permissions).

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

### Why this takes several pull requests

A button on the strength of the existing endpoint would be dishonest: it only locks the account. Real deletion needs a purge job, a trigger every service answers, a new way for a member to remove themselves, a scrub of personal data in the audit log, the call to Stripe, and the web flow. That is several services with their own tests plus an Identity job with retries, and a UI that deleted only part of it must not ship. The work is split (below); each step is reviewed before the next starts, and the web flow comes last because it is the one that makes the promise.

## Decision

The maintainer decided the six open points on 2026-11-10; they are binding.

### 1. Two operations

- **Delete the account** (owner): the whole account and everything of it in every service.
- **Delete me** (a member without the owner relation, sub-users included): only the user, their membership and their personal rows. Step 3.

**An owner of an account that still has other members cannot delete it.** The owner removes the members first (Settings -> Users); "Delete account" never removes other people implicitly. The API answers `409` and says how many members are left; the app (step 4) explains why and what to do. There is no ownership hand-over in the product (`promote_owner` is an operator command), so a hand-over is not part of this decision. The rule is `deletion_blocked_by` in the domain layer (`identity.domain.erasure`); other members count whatever their status, because a disabled member is still a person with an account row.

### 2. Re-authentication and who may call

Both operations need the step-up of ADR 0057 (password or passkey, in the native app the password) and a **login session**: API tokens and service identities get 403 `step-up-unavailable`, and a token is also refused by the owner check below. The existing `DELETE /api/v1/identity/accounts/{id}` keeps its body and its optional `password` field; a new `DELETE /api/v1/identity/me` (step 3) applies the policy by role so the client does not have to know which operation applies. The web flow (step 4) reuses the `StepUp` sheet, then an explanation, a typed confirmation and the date of the purge.

### 3. Grace period, cancelling, and the state machine

The grace period stays (`IDENTITY_DELETION_GRACE_DAYS`, default 7; 0 purges at the next sweep). The account moves through four states; only the first two are reversible:

```text
active --request--> deleting --grace over--> purging --every service confirmed--> erased
          ^             |
          +--- restore -+      (the owner, until the purge starts)
```

- **deleting** (`accounts.status`, `delete_after`): API tokens are revoked, the sessions of other members have ended (none are left: the owner is alone), and the account has **no OpenFGA tuples for anybody, the owner included**. Every service therefore refuses every request. This replaces the older behaviour (the owner kept the `owner` tuple and with it full access to mowers and settings during the grace period).
- **Cancelling, the smallest safe mechanism.** The owner can still **sign in** during the grace period (`_usable_account` lets the owner of a `deleting` account through, nobody else), and two Identity routes work for them: `POST /accounts/{id}/restore` and the repeated `DELETE` (409 "already scheduled"). Both use `AccountOwner`, a dependency that reads the owner from the database instead of asking OpenFGA, because OpenFGA holds nothing for a locked account. It refuses API tokens, services, non-owners (403) and other accounts (404). So the sign-in can do nothing but restore: every other route needs a tuple and answers 404, like for a foreign account. Restore re-grants the tuples. No cancel link and no new endpoint: the app shows the state from `GET /me` (the memberships carry the account status) and offers "Cancel deletion" (step 4). The restore and the start of the purge decide in one transaction each, so exactly one wins; once the purge has started, restore answers 409.
- **purging** (`accounts.status`, one `account_erasures` row): all sessions end, the tuples stay gone, the account is locked for everybody, there is no way back. The record names the services that must confirm, which did, the attempts and the next attempt.
- **erased**: the last service confirmed and Identity deleted its own rows in one transaction (the user, memberships, roles, tokens, passkeys, linked providers, sessions, devices, the account, the tuples). The `account_erasures` row stays as the proof (account id, times, services, counts; no personal data); `account.erased` is written to the audit log.

The rules (the transitions, the retry backoff, which accounts are due, which members block a deletion) are pure functions and the `AccountErasure` entity in `identity.domain.erasure`; the use case is `ErasureService` (`identity.application.erasure`); the bus is behind the `AccountEraser` port with `BusAccountEraser` as the adapter. There is no switch to turn deletion off.

### 4. The services are told over core NATS

Identity requests `account.erase.<service>` for each service with an `AccountEraseRequest{account_id, requested_at, reason}` and expects an `AccountEraseReply{ok, deleted, error}` (`libs/mowdeck-contracts`, `v1/erasure.py`, subjects in `v1/subjects.py`; the pattern of `entitlements.plan.set`, ADR 0031). Core NATS is reachable by services only (ADR 0074), so the request carries no principal. The permissions are in `tools/repo-checks/scripts/nats_auth.py`: Identity publishes `account.erase.*`, each service will serve its own subject in its step.

**Semantics every service keeps** (documented in the contract):

- **Idempotent:** erasing an account twice, or one the service never knew, is `ok=True` with `deleted=0`.
- **Retry safe:** a service that fails answers `ok=False` with a short `error`, or does not answer at all (NATS answers "no responders" at once when it is down); nothing is half acknowledged.
- **Completion is Identity's:** the reply is the confirmation. Identity stores it at once, never asks a confirmed service again, and deletes its own rows only after the last confirmation. An account that is gone from Identity is gone everywhere, and one that is not stays locked and visible.

**Order and timing.** Fleet is first: removing the mowers stops their simulators and connections. After the first confirmation the others wait `IDENTITY_ERASURE_SETTLE_SECONDS` (30) so the mowers' last states have landed (ADR 0076 does the same). A failure doubles the wait (30 s, 60 s, ... at most 10 minutes). One attempt holds the purge with a five minute lease, so two Identity replicas do not run the same one.

**Which services are asked** is a setting, `IDENTITY_ERASURE_SERVICES`, default `fleet,scheduler,telemetry,notifications,vision,planner,entitlements`. An installation that runs Billing adds `billing` (Compose profile `billing`; Billing is optional and an unanswered subject would hold every purge back). Until a service has its handler (steps 2a to 2c) Identity keeps the account in `purging` and logs the failure: **it never deletes its own rows while a listed service has not confirmed**, so a deployment that has this step alone is safe, only slow. A guard test (`tools/repo-checks/tests/test_erasure_registry.py`) lists the services that own tables and are not answering yet as `PENDING` with their step; the list must shrink to empty by step 2c, and a new service with tables that is neither answering nor pending fails the test.

**What the operator sees.** The record is `python -m identity.cli erasures [--all]`: state, services confirmed of those required, attempts and the last error. A failing step logs a warning on every attempt, and an error once the purge is older than `IDENTITY_ERASURE_STALL_HOURS` (1): "the account is locked but not deleted".

### 5. Billing

Billing (step 2c) cancels the open subscription at the provider (`BillingService.cancel`, already written) **and deletes the customer there**. Invoices the provider must keep stay with the provider; Billing keeps no invoice data of an erased account, and the privacy text and the operator documentation say so.

### 6. Audit log: rows stay, personal fields go

The audit logs are append-only by trigger (ADR 0076 added the one exception, the demo purge's `DELETE`). A deletion keeps its audit rows and removes their personal fields, through the **one sanctioned path** the trigger allows, defined in migration 0011:

- The trigger `audit_log_append_only()` lets an `UPDATE` pass only while the transaction-local setting `mowdeck.audit_scrub` is `on` **and** the new row differs from the old one in nothing but removed personal data: every column except `actor_name`, `ip` and `details` is equal (`IS NOT DISTINCT FROM`), `actor_name` and `ip` become NULL, and the new `details` is contained in the old one (`old @> new`, so it can only lose keys). Anything else, with the flag or without, raises "audit_log is append-only"; a `DELETE` still needs the demo purge's flag.
- Only the database function `audit_log_scrub(account_id)` sets the flag, and it clears it again before it returns, so a later statement of the same transaction is refused again. It keeps only the `details` keys on the allow list (ids, counts, kinds, times: `AUDIT_DETAILS_KEPT` in `identity.domain.erasure`; the migration holds the same list and a test compares both) and drops every other key, so a key a future action adds is scrubbed until someone lists it. The function is idempotent.
- The Python side is `Tx.scrub_audit(account_id)` (SQL: the function; memory: the same filter).
- The integrity protection is not weakened for anything else: no flag, no change; the demo purge's `DELETE` is untouched. Tests on PostgreSQL cover the function, every refused variant (with and without the flag) and that the flag does not leak out of the function.

The final row `account.erased` carries ids, counts and the services, no personal data. Opaque `created_by` ids in other services stay: once Identity's user row is gone they identify nobody. Other services' audit logs (Fleet's) are handled in their step.

### 7. Backups and the dialog text

There is no defined backup retention today (the operator's `pg_dumpall` before updates, [Compose](../ops/compose.md)), and backups are a separate topic the maintainer will take up later. **No retention is invented here.** The in-app dialog, the privacy template and the operator documentation say truthfully that backups made by the operator may still contain the data until they are rotated out and that the retention is up to the operator. Step 4 adds that text, with a clearly marked TODO in the operator documentation and the privacy template for the operator's retention statement.

### 8. Third parties

- **Stripe:** see 5.
- **APNs:** the app removes its registration on sign-out (`nativeShell.signingOut()`), and the purge deletes the rows, so no push goes to the device again.
- **Vendor clouds** (Mammotion, Husqvarna, Worx): MowDeck removes the stored credentials with the linked account and cannot delete what the vendor holds. The in-app text and the privacy text say so.
- **Cloud AI provider** ([ADR 0081](0081-explicit-consent-before-cloud-ai.md)): a request already sent stays with the provider under its terms; MowDeck deletes the pictures and keys it stores.

### 9. Web app (step 4)

Settings -> Profile gets a separate danger area under the sign-out ("Delete account" or "Delete me" by role, the explanation, the member count, a blocked state with its reason, the date of the purge, "Cancel deletion" while it is scheduled, `DemoLock` in the demo). The flow is a lib service (no rules in the component): load what applies, step-up through the existing controller, call the endpoint, then the same clean exit as sign-out (`nativeShell.signingOut()` first so push tokens and widget credentials go, then the session logout). Component and unit tests cover pending, error, blocked and demo-locked.

## Consequences

- Step 1 changes nothing the web app shows. It changes three behaviours of the API: `DELETE /accounts/{id}` answers 409 while other members exist; a deleting account gives the owner no tuples (the sign-in can only restore); and a purge now runs after the grace period. A deployment that has step 1 alone and has accounts scheduled for deletion keeps them `purging` until the services answer (a logged warning on every attempt, then an error), which is the safe direction.
- The App Store requirement stays open until step 4 ships; submitting earlier would fail review.
- A new service that stores per-account data must answer `account.erase.<service>`, and the guard test fails until it does (this extends the checklist of ADR 0076, which covers the demo only).
- The grace period means an account's data lives up to 7 days after the request plus the purge time; the text must say so.
- The PostgreSQL function and trigger of migration 0011 are the only way the audit log is ever changed, and only to remove personal data.

### The split

1. **Contract and Identity core** (this step): the contract and subjects, the state machine, the purge job and its record, Identity's own rows and tuples, cancel within the grace period, the audit scrub, the owner block, the guard test with `PENDING`.
2. **Service handlers** answering `account.erase.<service>`: 2a Fleet, Scheduler, Telemetry, Planner and Entitlements (the existing purge classes of ADR 0076 behind the new adapter); 2b Notifications and Vision; 2c Billing (cancel and delete at the provider). `PENDING` shrinks with each.
3. **Member self-removal:** `DELETE /identity/me`, the role policy, the user-level cleanup in Fleet, Telemetry and Vision, tests across accounts.
4. **Web app, docs and e2e:** the Settings flow with its states, the privacy template and the operator documentation (backup TODO), the Playwright spec, the README.
