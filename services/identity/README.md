# identity

Accounts, users, roles, sessions and tokens: who is who and who may do what. Identity registers accounts, logs users in, issues the short-lived access tokens every other service verifies on its own, manages sub-users, roles and API tokens, and writes the authorization tuples of its subjects to OpenFGA.

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; layer rules in `docs/dev/service-template.md`. Design: [Design: identity and authorization](../../docs/dev/design-identity-authz.md), sections 3 and 4. Decisions: [ADR 0014](../../docs/adr/0014-authorization-model-and-tuple-ownership.md) (authorization model, tuple ownership) and [ADR 0015](../../docs/adr/0015-tokens-sessions-and-key-rotation.md) (tokens, sessions, key rotation).

## Purpose

- **Accounts** (household or business) with an owner. Registration creates the account, the owner, the default roles and the owner's tuples. Deletion locks the account for a grace period (7 days) during which the owner can restore it; the purge job is a later task.
- **Users**: persons register; **sub-users** are created by an account with a username and a password (no e-mail needed) and belong to it. Usernames are unique per installation.
- **Roles** are named bundles of permission keys per account. Owner and Admin are fixed relations (`account#owner`, `account#admin`); Manager, Operator, Viewer and custom roles are editable. A role is assigned at a level (account, site, group, mower) and Identity writes the expanded `grant_*` tuples (ADR 0014).
- **Sessions**: password login, 10 minute EdDSA access tokens, opaque refresh tokens with rotation and reuse detection; for browsers a cookie-only refresh transport and a short reuse grace after a lost response ([ADR 0021](../../docs/adr/0021-browser-auth-flow.md)).
- **API tokens** (Home Assistant) with explicit grants capped at the creator's rights, exchanged for access tokens.
- **Personal access tokens** ([ADR 0064](../../docs/adr/0064-personal-access-tokens-and-public-api-docs.md)): every user's own API tokens for scripts, with curated scopes and a required expiry; documented for users in [Scripting MowDeck](../../docs/guides/scripting.md).
- **Audit log** of every permission change and every token issue, revocation or deletion, with the correlation id.
- **Security center** ([ADR 0065](../../docs/adr/0065-security-center-and-new-login-alerts.md)): every user sees and ends their own sessions, gets an alert on a login from a new device (table `known_devices`, cookie `mowdeck_device`, migration `0008`) and reads their own security events. See [Security center](#security-center-adr-0065) below.

## Interfaces

All routes are mounted under `/api/v1` (OpenAPI at `/api/v1/openapi.json`). Traefik routes `/api/v1/identity` and `/.well-known/jwks.json` to this service.

### Public

| Method | Path | Description |
| --- | --- | --- |
| POST | `/api/v1/identity/register` | `{username, password, account_name, display_name?, email?, account_kind?}`: account plus owner, 201. Registration mode decides who may (`open`, `closed`: only the first account) |
| POST | `/api/v1/identity/session` | Login `{username, password, account_id?}`: access token, refresh token (body and `HttpOnly` cookie; with the header `X-Mowdeck-Refresh: cookie-only` only the cookie, `refresh_token` in the body is `null`). 401 on any failure (one message), 429 when throttled |
| POST | `/api/v1/identity/session/refresh` | `{refresh_token}` or the cookie plus `X-Requested-With`: rotates; a reused token ends the whole session, except within the reuse grace (below). Accepts `X-Mowdeck-Refresh: cookie-only` like the login |
| POST | `/api/v1/identity/session/logout` | Ends the session of the refresh token, 204 |
| POST | `/api/v1/identity/token` | `{grant_type: "api_token", token}`: API token exchange, access token with `kind=api_token` |
| GET | `/api/v1/identity/login-methods` | `{password, passkey, github, apple}`: the login methods the installation offers (passkeys need `MOWDECK_IDENTITY_PUBLIC_URL`; GitHub also the client id and secret, Apple the Services ID, Team ID, Key ID and key file). Only booleans: no provider setting is ever published |
| POST | `/api/v1/identity/passkeys/challenge` | Start a passwordless login: `{ceremony_id, public_key}` (request options, user verification required, no `allowCredentials`). Looser rate limit (every sign-in page asks, for the autofill) |
| POST | `/api/v1/identity/passkeys/session` | `{ceremony_id, credential, account_id?}` (`PublicKeyCredential.toJSON()`): the same tokens and cookie as `POST /session`. 401 with one message for every failure, 429 when throttled |
| POST | `/api/v1/identity/app-login/start` | Native app hand-off, first step in the app: `{code_challenge}` (PKCE S256) gives `{request_id, expires_in}` (single use, 10 minutes). Looser rate limit like the passkey challenge |
| POST | `/api/v1/identity/app-login/session` | Native app hand-off, app side: `{code, code_verifier}` gives the app a session like `POST /session`; the code is single use |
| POST | `/api/v1/identity/oauth/{provider}/start` | Sign in with a login provider (`github`, `apple`): `{return_query?}` (the sign-in page's query; only `next`, `app_request`, `app_state` are kept) gives `{authorize_url, expires_in}` and sets the HttpOnly binding cookie `mowdeck_oauth` (`SameSite=Lax`, path `/api/v1/identity/oauth/`). 404 when the provider is off. Looser rate limit like the passkey challenge |
| GET | `/api/v1/identity/oauth/{provider}/callback` | The provider sends the browser back here (the callback URL registered in the OAuth App or at the Services ID). 303 to `/login?…&signed_in_with=<provider>` with the refresh cookie (a session as after `POST /session`), or `…&sign_in_error=<code>&provider=<provider>`; for a link to `/settings?linked=<provider>#profile` or `?link_error=<code>&provider=<provider>#profile`. Login rate limit |
| GET | `/.well-known/jwks.json` and `/api/v1/identity/.well-known/jwks.json` | Public signing keys (`Cache-Control: max-age=300`). Services inside Compose use `http://identity:8000/.well-known/jwks.json` |
| GET | `/healthz`, `/readyz`, `/metrics`, `/.well-known/mowdeck-module` | Operations (kit) |

### Authenticated

The account in the path must be the account of the token, otherwise 404. Then OpenFGA is asked for the relation on `account:<id>` with `member` as the visibility relation: members without the right get **403**, everybody else **404** (a foreign account never reveals its existence). Identity installs the token pre-filter of `mowdeck-authz` (a key missing from `perms` is denied without asking OpenFGA).

| Method | Path | Relation on the account | Notes |
| --- | --- | --- | --- |
| GET | `/api/v1/identity/me` | token only | Claims, user, memberships |
| PUT | `/api/v1/identity/me/password` | token only (users) | Current password required; other sessions end |
| POST | `/api/v1/identity/me/permissions` | token only | Batch `[{relation, object}]` to booleans, for the UI |
| GET | `/api/v1/identity/me/platform` | token only | `{"operator_admin": bool}` (`platform:main#operator_admin`, through the authorizer cache); the web app uses it instead of probing `/platform/accounts` |
| GET | `/api/v1/identity/ui-areas` | token only | Areas of the web app shell: the `ui_areas` of Identity's own descriptor and of the last descriptor every module URL answered with, filtered to the areas whose `required_permission` the caller holds; each with `module`, `module_version`, `route`, `menu_label`, `labels`, `required_permission`. `Cache-Control: private, max-age=300` |
| POST | `/api/v1/identity/session/logout-all` | token only (users) | Ends every session of the caller |
| GET | `/api/v1/identity/me/passkeys` | token only (users) | The caller's passkeys (no key material) |
| GET | `/api/v1/identity/me/api-tokens` | token only (users) | The caller's personal access tokens (ADR 0064), newest first, active and revoked, never the secret: `{id, name, prefix, scopes, level, created_at, expires_at, last_used_at, revoked_at}`. API tokens: 403 |
| POST | `/api/v1/identity/me/api-tokens` | token only (users) | `{name, scopes, expires_in_days, level?}`: a personal access token, 201 with `token` (the secret, once) and `Cache-Control: no-store`. Scopes from `mower.view`, `mower.control`, `schedule.edit` (`mower.view` required with any other; 422 otherwise), each held by the caller on `level` (default the account; 403 otherwise); `expires_in_days` 1 to 365 (required); at most 25 active per user (409). **Needs a recent step-up**; API tokens: 403 |
| DELETE | `/api/v1/identity/me/api-tokens/{token_id}` | token only (users) | Revoke an own personal access token, 204 (idempotent); 404 for any other token. API tokens: 403 |
| POST | `/api/v1/identity/me/step-up/password` | token only (users) | Step-up (ADR 0057): `{password}` gives a new access token with `auth_time` = now (login limits, failures counted; audited `step_up.succeeded` / `step_up.reauth_failed`). API tokens: 403 |
| POST | `/api/v1/identity/me/step-up/passkey/options` | token only (users) | Step-up with a passkey: options (user verification required) and `ceremony_id` |
| POST | `/api/v1/identity/me/step-up/passkey` | token only (users) | `{ceremony_id, credential}`: must be a passkey of the caller; a new access token with `auth_time` = now (`amr` `passkey`) |
| POST | `/api/v1/identity/me/passkeys/options` | token only (users) | `{password?}` (re-authentication, the username throttle applies; without it a recent step-up is required): creation options and `ceremony_id` |
| POST | `/api/v1/identity/me/passkeys` | token only (users) | `{ceremony_id, credential, name?}`: verifies and stores the passkey, 201; 409 for a known credential or more than 20 |
| PATCH, DELETE | `/api/v1/identity/me/passkeys/{passkey_id}` | token only (users) | Rename `{name}`; remove (the password always stays; **needs a recent step-up**) |
| GET | `/api/v1/identity/me/external-identities` | token only (users) | The caller's linked provider accounts: `[{provider, display_name, linked_at, last_used_at}]` |
| POST | `/api/v1/identity/me/external-identities/{provider}/link` | token only (users) | `{password}` (re-authentication, the login limits apply): `{authorize_url, expires_in}` and the binding cookie; 409 when an account of that provider is already linked |
| DELETE | `/api/v1/identity/me/external-identities/{provider}` | token only (users) | Unlink (always allowed, also with the provider turned off; the password stays), 204; 404 when nothing is linked |
| GET | `/api/v1/identity/me/security` | token only (users) | Security center ([ADR 0065](../../docs/adr/0065-security-center-and-new-login-alerts.md)): `{password_set, password_changed_at, must_change_password, new_login_alerts, passkeys, providers, personal_tokens, sessions, known_devices}`. `Cache-Control: no-store`. API tokens: 403 |
| PUT | `/api/v1/identity/me/security/alerts` | token only (users) | `{new_login_alerts}`: the new-device alert on or off; turning it off **needs a recent step-up** (audited `security.alerts_changed`) |
| GET | `/api/v1/identity/me/sessions` | token only (users) | The caller's live login sessions in every account, the current one first: `{id (the sid), account_id, current, client, browser, os, label, ip_prefix, network, created_at, last_used_at, expires_at, method}`. Only the caller's own; no admin view. API tokens: 403 |
| DELETE | `/api/v1/identity/me/sessions/{session_id}` | token only (users) | End one own session (every row of the family revoked, audited `session.revoked`), 204, idempotent; 404 for any other session. **Needs a recent step-up** |
| POST | `/api/v1/identity/me/sessions/revoke-others` | token only (users) | End every own session but the current one: `{revoked}` (audited `session.revoked_others`). **Needs a recent step-up** |
| GET | `/api/v1/identity/me/security-events` | token only (users) | The caller's own security events of the last `days` (1 to 90, default 30), newest first, at most `limit` (default 100): `{id, at, action, outcome, target_type, details, ip_prefix, network}` with a safe subset of the details and the address shortened |
| GET | `/api/v1/identity/me/onboarding` | token only (users) | The caller's setup tour progress (ADR 0062): `{status, skipped_steps, dismissed_hints, updated_at}`; `status` `new` (nothing stored), `active`, `dismissed` or `completed`. `Cache-Control: no-store`. API tokens: 403 |
| PUT | `/api/v1/identity/me/onboarding` | token only (users) | Replace it: `{status, skipped_steps?, dismissed_hints?}` (`status` not `new`; keys `^[a-z][a-z0-9-]{0,31}$`, at most 32 each, duplicates dropped). Whether a step is done is never stored; the web app reads it from the services |
| POST | `/api/v1/identity/app-login/code` | token only (users) | Native app hand-off, browser side: `{request_id}` of the app's request (unknown, used or expired: 403). Ends the caller's login session, which must be younger than 5 minutes (else 403), and returns a one-time code `{code, expires_in}` (60 s) bound to the request's challenge |
| GET | `/accounts/{account_id}` | `member` | |
| PATCH | `/accounts/{account_id}` | `can_manage_users` | Name |
| DELETE | `/accounts/{account_id}` | `can_manage_account` (owner) | `{password}`; 202, status `deleting` with `delete_after`; API tokens revoked, other members' sessions end, their tuples removed |
| POST | `/accounts/{account_id}/restore` | `can_manage_account` | Undo within the grace period |
| POST | `/accounts/{account_id}/reconcile` | `can_manage_users` | Make OpenFGA match the database for every subject of the account |
| GET | `/accounts/{account_id}/permissions` | `member` | Permission catalogue |
| GET, POST | `/accounts/{account_id}/users` | `can_manage_users` | List; create a sub-user with optional `assignments` |
| GET | `/accounts/{account_id}/users/{user_id}` | `member`, and `can_manage_users` unless it is the caller | |
| PATCH | `/accounts/{account_id}/users/{user_id}` | `can_manage_users` | Display name (sub-users, or yourself) |
| POST | `/accounts/{account_id}/users/{user_id}/disable`, `/enable` | `can_manage_users` | Disable ends sessions, revokes the API tokens the user created, removes tuples |
| POST | `/accounts/{account_id}/users/{user_id}/password` | `can_manage_users` | Temporary password of a sub-user |
| DELETE | `/accounts/{account_id}/users/{user_id}` | `can_manage_users` | Deletes a sub-user, removes a person from the account |
| GET | `/accounts/{account_id}/users/{user_id}/passkeys` | `can_manage_users` | Passkeys of a sub-user of the account (persons: 403, they manage their own) |
| DELETE | `/accounts/{account_id}/users/{user_id}/passkeys/{passkey_id}` | `can_manage_users` | Remove a sub-user's passkey (audit `passkey.removed` with `by_admin`) |
| GET | `/accounts/{account_id}/roles` | `member` | |
| POST | `/accounts/{account_id}/roles` | `can_manage_users` | Custom role; never more than the actor holds on the account |
| PATCH, DELETE | `/accounts/{account_id}/roles/{role_id}` | `can_manage_users` | System roles are immutable; an edit reconciles every holder; delete refused while assigned |
| GET | `/accounts/{account_id}/role-assignments?subject_id=` | `member` for own, else `can_manage_users` | |
| POST | `/accounts/{account_id}/role-assignments` | `can_manage_users` | `{subject_id, role_id, level:{type,id}}` with anti-escalation |
| DELETE | `/accounts/{account_id}/role-assignments/{id}` | `can_manage_users` | |
| GET, POST | `/accounts/{account_id}/api-tokens` | `can_manage_users` | Create returns the secret once; users only |
| DELETE | `/accounts/{account_id}/api-tokens/{token_id}` | `can_manage_users` | Revoke. With `?purge=true`: delete an already revoked token for good (204; 409 while it is not revoked, so an active token is always revoked first) |
| POST | `/accounts/{account_id}/api-tokens/purge-revoked` | `can_manage_users` | Delete every revoked token of the account for good: `{deleted, ids}`; active tokens stay |
| GET | `/accounts/{account_id}/audit` | `can_view_audit` | Filters `action`, `actor`, `target_id`, `since`; pages with `before_id`, `limit` |

(`/accounts/...` paths are under `/api/v1/identity`.)

Rules enforced by the use cases:

- **Anti-escalation.** Assigning a role at a level needs every effective key of the role on that level (asked in OpenFGA as the computed relation, so an Operator on group North cannot hand out Manager there); creating or editing a role needs its keys on the whole account; an API token grant needs its keys on its level. Only the owner assigns or revokes Admin; nobody assigns Owner. Levels below the account must belong to the account (Fleet's `account` tuple), else 404.
- **Role bundling.** Every `mower.*` and `schedule.*` key needs `mower.view` in the same role. Keys that do not exist on a level are not granted there.
- **API tokens** can never hold `users.manage`, `billing.manage` or `audit.view`.
- **Personal access tokens** ([ADR 0064](../../docs/adr/0064-personal-access-tokens-and-public-api-docs.md)) are API tokens with `purpose` `personal` (column `api_tokens.purpose`, migration `0007`; every other token is `integration`, which `GET .../api-tokens` shows as `purpose`). Any user creates them for themselves under `/me/api-tokens` with the curated scopes `mower.view`, `mower.control` and `schedule.edit` (never more than they hold), an expiry of 1 to 365 days and a recent step-up; at most 25 active per user. They are exchanged and revoked like any API token (revoked with their creator too), listed in the account's token list for admins, and no API token can list, create or revoke them (403). Audit: `api_token.personal_created`.
- **Widget credentials** for the iOS widgets ([ADR 0059](../../docs/adr/0059-ios-widgets-and-live-activity.md)): any user creates an API token `iOS widgets: <device>` for themselves (`POST /me/widget-credentials`) with `mower.view` and `mower.control` on the account, as far as they hold them, 90 days, at most 10 active, rotation with `replaces`; listed and revocable like any token, revoked with its creator.
- **Revoked API tokens** stay listed until the owner deletes them (there is no automatic retention). Only revoked tokens can be deleted, one (`?purge=true`) or all of the account (`purge-revoked`); each deleted token is audited as `api_token.purged` with its name, prefix, revocation time and reason (never the secret), and its subject is reconciled once more so no tuple outlives the row. Nothing references `api_tokens` by foreign key: `audit_log` keeps the token id as text and is append-only, so the token's history (`api_token.created`, `api_token.exchanged`, `api_token.revoked`) stays.
- **Guards.** The owner cannot be disabled or removed; only the owner disables or removes an admin; nobody disables or removes themselves.

### Browser sessions

The web app keeps the access token in memory and the refresh token only in the cookie `mowdeck_refresh` (`HttpOnly`, `SameSite=Strict`, `Path=/api/v1/identity/session`, `Secure` from `MOWDECK_IDENTITY_REFRESH_COOKIE_SECURE`): it sends `X-Mowdeck-Refresh: cookie-only` on login and refresh, so no response body carries the refresh token. Without the header the body keeps it (scripts, Home Assistant). Identity stores only hashes of refresh tokens, so the reuse grace cannot return the successor that was already issued; it rotates that unused successor instead and sets the new cookie. Traefik limits refresh and logout separately from login (router `identity-session`, 60 per minute, burst 30). See [ADR 0021](../../docs/adr/0021-browser-auth-flow.md).

### Permission catalogue

The catalogue holds every key of the authorization model (always present, with the levels the model allows) plus the keys of the module descriptors fetched from `MOWDECK_IDENTITY_MODULE_URLS` at startup (with retries; every 10 minutes after that, every 30 s while a module is missing) and Identity's own descriptor. Keys without a relation in the model are **account keys**: they can only be granted on the whole account and are enforced by their module from the token's `perms`. A descriptor may say a key is implied by others (`implied_by`): Device-Gateway's `device_gateway.view` is implied by `mower.view` on the whole account. Unknown keys in roles and tokens are rejected (422). Keys that disappear from a module that answered are marked retired.

### Token claims

`sub`, `acc`, `kind` (`user`, `subuser`, `api_token`), `name`, `roles`, `perms` (keys held anywhere in the account: an upper bound for pre-filters, never a grant), `plan` (asked from Entitlements at issue through the kit client with its cache; `unlimited` while billing is disabled, also when Entitlements does not answer; empty with billing enabled and no answer; informational, services decide through the client, [ADR 0028](../../docs/adr/0028-entitlements-plans-and-enforcement.md)), `sid` (login session), `jti`, `iss` (`MOWDECK_IDENTITY_ISSUER`), `aud` (`mowdeck-api`), `iat`, `nbf`, `exp` (10 minutes). Header `{"alg": "EdDSA", "kid", "typ": "at+jwt"}`.

## Step-up re-authentication (ADR 0057)

Critical actions need the user to have confirmed with a passkey or the password within `MOWDECK_AUTH_STEP_UP_MAX_AGE_SECONDS` (default 300, 30 to 3600; set the same value for every service). The access token carries `auth_time` and `amr`; they live on the login session (`sessions.auth_time`, `auth_method`), a login sets them (password, passkey, GitHub, Apple), a refresh copies them and `/me/step-up/*` renews them with a new access token. Identity's own routes use `FreshAuth` (`require_recent_auth()` of `mowdeck-auth`): without a recent confirmation the answer is `401 urn:mowdeck:problem:reauth-required` (`WWW-Authenticate: Bearer error="insufficient_user_authentication", max_age="300"`), for API tokens `403 urn:mowdeck:problem:step-up-unavailable`. Protected here: create an API token or a personal access token, create/delete a user or sub-user, set a sub-user's password, assign/unassign roles and change a role, remove a passkey (own or a sub-user's), unlink GitHub/Apple, end one of your sessions or all others, turn off new-login alerts (ADR 0065); adding a passkey, linking a provider and deleting the account accept the old `password` field or, without it, a recent step-up. Fleet protects deleting a mower and removing a linked cloud account the same way. Audit: `step_up.succeeded`, `step_up.failed`, `step_up.reauth_failed`.

## Security center (ADR 0065)

- **Sessions** are refresh token families (`sid`). Ending one revokes every row of the family: its refresh token fails at once, Identity's own endpoints refuse its access tokens at once (the request context checks the family of the `sid`; unknown families pass), every other service accepts them until they expire (at most `MOWDECK_IDENTITY_ACCESS_TOKEN_MINUTES`, default 10). The same check now applies to sessions ended by logout, "log out everywhere", a password change or the app hand-off.
- **Devices** are told apart by the HttpOnly cookie `mowdeck_device` (a random id, `SameSite=Lax`, path `/api/v1/identity`, 400 days, `Secure` like the refresh cookie) set at every login; `known_devices` keeps its SHA-256 per user with a label from the user agent (`Firefox on Windows`, `MowDeck app on iOS`; no versions), first and last seen and the shortened address of the last login. No address leaves the server: addresses are shortened to `a.b.c.x` or an IPv6 `/48`, private ones are the home network.
- **New-login alert**: a login from a device without a row of the user is audited `security.new_device` and publishes `identity.session.new_device` when the user has `new_login_alerts` on (default), already had another device (first logins are silent) and the login is not the app hand-off. Notifications sends `security_new_login` to that user only.
- **Password status**: `users.password_changed_at` is set by registration, a password change and an admin's reset (NULL for users from before migration `0008`).
- **Unlink guard**: unlinking a login provider answers 409 when no password, no passkey and no other provider would remain.
- The provider callback of a link redirects to `/settings?linked=<provider>#security` (`…link_error=…#security`).
## Events

Produced on JetStream `EVENTS`, payloads in `mowdeck_contracts.v1.identity` (JSON Schemas in `libs/mowdeck-contracts/schemas/v1/`), published after the commit:

| Subject | Payload | When |
| --- | --- | --- |
| `event.identity.account.created` | `AccountCreated` | registration |
| `event.identity.user.created` | `UserChanged` | registration, new sub-user |
| `event.identity.user.disabled` | `UserChanged` | user or membership disabled |
| `event.identity.user.deleted` | `UserChanged` | sub-user deleted, person removed |
| `event.identity.grants.changed` | `GrantsChanged` | tuples of subjects changed: drop cached decisions (`mowdeck_authz.events.subscribe_cache_invalidation`) |
| `event.identity.api_token.revoked` | `ApiTokenRevoked` | token revoked (also by disable, removal, account deletion) |
| `event.identity.session.new_device` | `NewDeviceLogin` | a login from a device the user had not used before, alerts on (ADR 0065) |

Consumed: `event.identity.grants.changed` (from other Identity instances) to clear the own decision cache.

## Consistency with OpenFGA

The database is the source of truth; OpenFGA is a projection per subject. Every change runs in one database transaction; right before the commit Identity computes the desired tuples of the touched subjects from the database (memberships, owner and admin flags, role assignments expanded to `grant_*`, token grants), reads their current tuples from OpenFGA and writes only the difference. If OpenFGA cannot be read or written, the transaction rolls back and the request answers **503**: nothing is half applied. If the commit fails after the OpenFGA write, the touched subjects are reconciled again from the committed state at once. Remaining drift is repaired by `POST /accounts/{id}/reconcile`, `python -m identity.cli reconcile` and a periodic full reconcile (`MOWDECK_IDENTITY_RECONCILE_SECONDS`, default hourly). This replaces the outbox table of the design: the write-through is simpler and gives the 503 the plan asks for; the outbox remains an option if Identity ever needs to accept changes while OpenFGA is down.

## Settings

All settings of `mowdeck-service-kit` apply (`MOWDECK_*`, any value also from `MOWDECK_<NAME>_FILE`). OpenFGA: `MOWDECK_OPENFGA_*` of `mowdeck-authz`. Service specific settings:

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_DATABASE_URL` | required | PostgreSQL of the `identity` database |
| `MOWDECK_IDENTITY_KEY_ENCRYPTION_KEY` | required | 256-bit key (base64 or hex) that encrypts the signing keys at rest; use `_FILE` in production |
| `MOWDECK_IDENTITY_REGISTRATION` | `open` | `open` (home installation default), `closed` (only the first account, then 403), `invite` (reserved, behaves like `closed`) |
| `MOWDECK_IDENTITY_ISSUER` | `urn:mowdeck:identity` | `iss`; services set `MOWDECK_AUTH_ISSUER` to the same value |
| `MOWDECK_IDENTITY_AUDIENCE` | `mowdeck-api` | `aud` |
| `MOWDECK_IDENTITY_ACCESS_TOKEN_MINUTES` | `10` | 1 to 15 |
| `MOWDECK_IDENTITY_REFRESH_IDLE_DAYS` | `7` | Unused refresh tokens expire |
| `MOWDECK_IDENTITY_REFRESH_ABSOLUTE_DAYS` | `30` | Maximum session length |
| `MOWDECK_IDENTITY_REFRESH_REUSE_GRACE_SECONDS` | `10` | 0 to 60, 0 disables. A rotated refresh token presented again within this time while its successor was never used counts as a lost response: the unused successor is rotated in its place (audit `session.refresh_grace`). Only the immediate predecessor of the newest token qualifies; anything else revokes the family (`session.refresh_reuse`) |
| `MOWDECK_IDENTITY_KEY_ROTATION_DAYS` | `30` | Age of the active key after which a next key is announced |
| `MOWDECK_IDENTITY_MODULE_URLS` | empty | Comma separated base URLs whose module descriptors define permissions |
| `MOWDECK_IDENTITY_CATALOGUE_REFRESH_SECONDS` | `600` | Descriptor refresh interval |
| `MOWDECK_IDENTITY_ARGON2_PROFILE` | `rfc9106_low` | `rfc9106_low` (64 MiB, t=3, p=4) or `owasp_min` (19 MiB, t=2, p=1); both meet the OWASP minimum |
| `MOWDECK_IDENTITY_ARGON2_CONCURRENCY` | `4` | Parallel hashes (memory = concurrency x profile memory) |
| `MOWDECK_IDENTITY_LOGIN_IP_PER_MINUTE` | `10` | Login and token exchange rate per client IP |
| `MOWDECK_IDENTITY_LOGIN_IP_BURST` | `20` | Burst per client IP |
| `MOWDECK_IDENTITY_LOGIN_MAX_FAILURES` | `10` | Failures per username within the window before 429 |
| `MOWDECK_IDENTITY_LOGIN_FAILURE_WINDOW_SECONDS` | `900` | Window of the username throttle |
| `MOWDECK_IDENTITY_TRUST_FORWARDED_FOR` | `false` | Client IP from `X-Forwarded-For` (only behind a trusted proxy; Compose sets it), by the kit's rule with `MOWDECK_CLIENT_IP_DEPTH` (`GATEWAY_CLIENT_IP_DEPTH`; unset: the leftmost entry) |
| `MOWDECK_IDENTITY_DELETION_GRACE_DAYS` | `7` | Grace period of an account deletion |
| `MOWDECK_IDENTITY_DEFAULT_PLAN` | `unlimited` | Plan of new accounts while billing is off |
| `MOWDECK_IDENTITY_REFRESH_COOKIE_SECURE` | `true` | `Secure` flag of the refresh cookie (Compose development: `false`) |
| `MOWDECK_IDENTITY_PUBLIC_URL` | empty | The address users open MowDeck at (`https://mowdeck.example.org`). Passkeys are bound to it: its host is the RP ID, its origin the only accepted one. HTTPS (plain HTTP only for `localhost`), a host name (no IP). Empty or unusable (logged): passkeys off |
| `MOWDECK_IDENTITY_PASSKEY_RP_ID` | empty | A parent domain of that host as the RP ID (`example.org`); empty: the host |
| `MOWDECK_IDENTITY_PASSKEY_RP_NAME` | `MowDeck` | Name authenticators show next to the passkey |
| `MOWDECK_IDENTITY_GITHUB_CLIENT_ID` | empty | Client ID of the GitHub OAuth App; empty: GitHub login off |
| `MOWDECK_IDENTITY_GITHUB_CLIENT_SECRET` | empty | Its client secret; give it as `MOWDECK_IDENTITY_GITHUB_CLIENT_SECRET_FILE`. GitHub is on only with id, secret and a usable `MOWDECK_IDENTITY_PUBLIC_URL` (the callback is `<public URL>/api/v1/identity/oauth/github/callback`); the startup log says `GitHub login is on` with that URL, or why it is off |
| `MOWDECK_IDENTITY_GITHUB_AUTHORIZE_URL`, `_TOKEN_URL`, `_API_URL` | github.com | GitHub's endpoints; other values only for GitHub Enterprise Server (`https://<host>/api/v3`) or the test stack's fake |
| `MOWDECK_IDENTITY_APPLE_SERVICES_ID`, `_TEAM_ID`, `_KEY_ID` | empty | Services ID, Team ID and Key ID of Sign in with Apple; empty: Apple login off |
| `MOWDECK_IDENTITY_APPLE_PRIVATE_KEY_FILE` | empty | Path of the Sign in with Apple key (`.p8`). Apple is on only with all four, a readable EC P-256 key and a usable `MOWDECK_IDENTITY_PUBLIC_URL` (the return URL is `<public URL>/api/v1/identity/oauth/apple/callback`); the startup log says `Apple login is on` with that URL, or why it is off |
| `MOWDECK_IDENTITY_APPLE_PKCE` | `true` | Send the PKCE challenge and verifier to Apple (Apple's support is unconfirmed; `false` only if Apple refuses the request) |
| `MOWDECK_IDENTITY_APPLE_AUTHORIZE_URL`, `_TOKEN_URL`, `_KEYS_URL`, `_ISSUER` | appleid.apple.com | Apple's endpoints; other values only for tests |
| `MOWDECK_IDENTITY_DEMO_USERNAME` | `demo` | Public demo (`MOWDECK_DEMO_MODE`, [ADR 0068](../../docs/adr/0068-public-demo-mode.md)): the user every visitor signs in as with `POST /demo/session` ("Try the demo"); the seed job creates it with `POST /demo/seed`. Demo mode also closes registration and turns passkeys, GitHub and Apple off, and Identity then refuses every mutation but the demo sign-in, refresh, logout, the permission check and the setup tour |
| `MOWDECK_IDENTITY_DEMO_ACCOUNT_NAME`, `_DEMO_DISPLAY_NAME` | `MowDeck demo`, `Demo visitor` | Names of the demo account and its user |
| `MOWDECK_IDENTITY_MAINTENANCE_SECONDS` | `60` | Key rotation, key reload, purge of login attempts older than 30 days and of expired passkey challenges, app login requests and codes, and GitHub round trips |
| `MOWDECK_IDENTITY_RECONCILE_SECONDS` | `3600` | Full reconcile interval, 0 disables |

### Passwords

Argon2id via `argon2-cffi` 25.1.0. Default `rfc9106_low`: 64 MiB memory, 3 iterations, 4 lanes (RFC 9106 second recommendation); `owasp_min`: 19 MiB, 2 iterations, 1 lane (OWASP Password Storage Cheat Sheet minimum). 16 byte salt, 32 byte hash. Hashing runs in worker threads, at most `ARGON2_CONCURRENCY` at a time. Hashes made with other parameters are upgraded after the next successful login. Unknown usernames run a dummy verification. Policy: 10 to 256 characters, no composition rules, not on a small list of common passwords, not the username. Refresh and API token secrets are 256-bit random values stored as SHA-256 and compared in constant time.

## Authentication plug-ins

`identity.application.authenticators`: `CredentialAuthenticator` (`PasswordAuthenticator` for `POST /session`, `ApiTokenAuthenticator` for `POST /token`, `PasskeyAuthenticator` for `POST /passkeys/session`, which is also a `ChallengeAuthenticator`: it hands out a single-use challenge first) and `RedirectAuthenticator` (a browser round trip to a provider: `OAuthAuthenticator` for any OAuth 2 provider on the port `OAuthClient`, `GitHubAuthenticator` with the GitHub client of `adapters/github.py`, registered when GitHub is configured; a later OIDC provider is another `OAuthClient`). The `AuthenticatorRegistry` holds the enabled ones by name. Re-authentication before a change of the own login methods (adding a passkey, linking GitHub) is `application/reauth.py`.

### Passkeys

[ADR 0050](../../docs/adr/0050-passkeys-and-app-login-hand-off.md). WebAuthn with py_webauthn 3.0.1 (BSD-3-Clause) behind the port `PasskeyCeremonies` (`adapters/webauthn.py`). Discoverable credentials, user verification required, attestation `none`, ES256/EdDSA/RS256; the user handle is the user id. Challenges (32 random bytes) live 5 minutes in `webauthn_challenges` and are consumed by the first answer. A login finds the passkey by credential id, checks the user handle, verifies the assertion and refuses a signature counter that did not grow when either counter is non-zero (cloned authenticator; synced passkeys keep 0). The session is the same as after a password login (`session.login` with `method: passkey`, plus `passkey.used`). Passkey failures are audited (`session.login_failed`, `method: passkey`, reason) but write no `login_attempts` row, so they never throttle the password login; the IP limit applies. The password always stays; removing the last passkey is allowed. Audit: `passkey.added`, `passkey.renamed`, `passkey.removed`, `passkey.used`, `passkey.reauth_failed`.

### Native app hand-off

The apps sign in through the system browser and take over the login: the app registers the S256 hash of a verifier (`POST /app-login/start`, table `app_login_requests`) and opens `/login?app_request=<id>&app_state=<random>`, the user signs in there by any method and confirms "continue in the app", the page exchanges its fresh session at `POST /app-login/code` for a one-time code (the browser session ends, audit `app_login.code_issued`), navigates to `mowdeck://auth/callback?code=&state=` (Android: an `intent:` URL for the package `app.mowdeck.mobile`), and the app redeems code and verifier at `POST /app-login/session` (`session.login` with `method: app_handoff`). Codes are 256-bit, SHA-256 at rest in `app_login_codes`, valid 60 s, deleted on the first attempt. Login-method agnostic: a redirect authenticator only has to start a browser session; the page then hands it over the same way.

### Sign in with GitHub

[ADR 0051](../../docs/adr/0051-sign-in-with-github.md), operations in [docs/ops/github-login.md](../../docs/ops/github-login.md). Authorization code flow with PKCE S256 and scope `read:user`. A round trip is a single-use row in `oauth_states` (SHA-256 of the random `state`, S256 hash of the PKCE verifier, purpose `login` or `link`, the user of a link, the sign-in page's query), valid 10 minutes; the verifier itself is the HttpOnly cookie `mowdeck_oauth`, so only the browser that started it can finish it. The callback consumes the row, redeems the code with the verifier, reads GitHub's numeric user id and login from `GET /user` and drops the GitHub token (never stored or logged, and not revoked, so GitHub keeps the user's authorization and does not ask for consent at every sign-in; ADR 0051 amendment 2026-10-25). Only accounts in `external_identities` (unique per GitHub account and per user) sign in, into the same session as a password login (`session.login` with `method: github`); there is no e-mail matching and no registration through GitHub. Linking needs the password again (`external_identity.reauth_failed` on a wrong one); unlinking is always allowed. Failures are audited (`session.login_failed`, `method: github`; `external_identity.link_failed`) without `login_attempts` rows. Audit: `external_identity.linked`, `external_identity.unlinked`. The app signs in with GitHub through the hand-off below, unchanged.

### Sign in with Apple

[ADR 0052](../../docs/adr/0052-sign-in-with-apple.md), operations in [docs/ops/apple-login.md](../../docs/ops/apple-login.md). The second `OAuthClient` behind the same `OAuthAuthenticator`: everything of the round trip, the links, the limits and the audit entries is the GitHub code. Differences: no scope (so Apple answers with a redirect and the `SameSite=Lax` binding cookie comes along), a `nonce` (the PKCE challenge of the round trip) that the ID token must carry, a client secret that Identity signs itself (ES256 JWT, one hour, from the `.p8` key), and the account is read from the validated ID token (Apple's key set cached and refreshed on an unknown `kid`; issuer, audience = Services ID, expiry and nonce checked), `sub` being the subject. The stored label is "Apple ID"; e-mail claims are ignored. Apple's tokens are dropped, not revoked. Test fake: `tests/apple_support.py`.

## Signing keys and rotation

Ed25519 keys live in the `signing_keys` table, private halves encrypted with AES-256-GCM (the kid as associated data) under the key-encryption key. The first key is created at startup. The maintenance loop announces a `next` key when the active key is older than `KEY_ROTATION_DAYS`, promotes it after an hour (services cache the JWKS for 5 minutes) and retires the previous key an hour later. Identity verifies its own tokens from the same keys without HTTP.

## CLI

`python -m identity.cli <command>` with the service settings (Compose: `docker compose exec identity python -m identity.cli <command>`). Passwords come from `MOWDECK_IDENTITY_CLI_PASSWORD`, a pipe, or a prompt.

| Command | Effect |
| --- | --- |
| `create-user USERNAME --account-name NAME [--display-name] [--email]` | Register an account with this owner, also when registration is closed |
| `promote-owner USERNAME --account ID [--keep-current]` | Make a member the owner; former owners become admins unless kept |
| `rotate-keys [--now]` | Announce a next key, or with `--now` activate a new key immediately |
| `retire-key KID` | Retire a key at once (emergency); tokens it signed fail |
| `list-keys` | Keys and status |
| `reconcile [--account ID]` | Make OpenFGA match the database |
| `grant-platform-admin USERNAME [--revoke]` | Make a user an operator admin: writes (or deletes) `platform:main#operator_admin`; operator admins manage accounts and plans (`can_manage_accounts`, `can_manage_plans`) and see no mower data. Audited (`platform.admin_granted`/`_revoked`). The tuple lives only in OpenFGA (reconcile leaves it alone); grant again after rebuilding the store |

### Operator level

`GET /api/v1/identity/platform/accounts?offset&limit` (operator admins only: `can_manage_accounts` on `platform:main`, account owners get 403): every account of the installation, oldest first, with id, name, kind, status, creation time and the owner's username; no mowers, no member list. The backend of the operator console (Phase 4).

### Plans

Sub-user creation checks the plan limit `max_users` (current: the account's memberships) through the Entitlements client: 409 `urn:mowdeck:problem:plan-limit` with the limit and the current count in `errors`. While billing is disabled every account has the plan `unlimited`.

## Data model

Tables (Alembic revisions `0002` to `0004`): `accounts`, `users`, `memberships` (owner and admin flags, status), `roles` (permission keys as JSON, kind), `role_assignments` (subject, role, level), `sessions` (hashed refresh tokens, family, rotation), `api_tokens` (hashed secret, grants as JSON), `signing_keys`, `audit_log` (append-only: a trigger rejects `UPDATE` and `DELETE`), `login_attempts`, `permission_catalogue`, `passkeys` (credential id, COSE public key, sign count, transports, AAGUID, name, backup flags, created, last used, user agent of the registration), `webauthn_challenges` (single use, 5 minutes), `app_login_requests` (the app's sign-in requests with the PKCE challenge, 10 minutes) and `app_login_codes` (hashed one-time codes of the app hand-off, 60 s), `external_identities` (GitHub accounts linked to users: provider, GitHub's user id, login, linked, last used) and `oauth_states` (single-use round trips to GitHub, 10 minutes); `support_grants` and `invitations` exist as schema only.

## Layers

| Package | Content |
| --- | --- |
| `domain/` | entities, permission rules (catalogue, role validation, token claims, anti-escalation helpers), credentials, errors |
| `application/` | use cases (`accounts`, `sessions`, `users`, `roles`, `api_tokens`, `audit`), OpenFGA sync, unit of work, keys, catalogue, authenticators, rate limiter, ports |
| `adapters/api/` | routes, schemas, dependencies, error mapping |
| `adapters/db/` | tables, the PostgreSQL store, Alembic migrations |
| `adapters/memory.py` | the memory store (tests) |
| `adapters/crypto.py`, `bus/`, `modules.py` | Argon2id, AES-GCM, event publisher, descriptor fetcher |
| `runtime.py`, `main.py`, `cli.py` | wiring, app, operator commands |

## Local start

With the Compose stack (builds the image, runs the migrations, waits for health):

```bash
cp infra/compose/.env.example infra/compose/.env
docker compose -f infra/compose/docker-compose.yml up -d --build --wait
curl -X POST http://localhost/api/v1/identity/register -H 'Content-Type: application/json' \
  -d '{"username": "lukas", "password": "correct horse battery", "account_name": "Home"}'
curl -X POST http://localhost/api/v1/identity/session -H 'Content-Type: application/json' \
  -d '{"username": "lukas", "password": "correct horse battery"}'
```

Container image (build context is the repository root):

```bash
docker build -f services/identity/Dockerfile -t mowdeck/identity:dev .
```

## Tests

```bash
uv run pytest services/identity --cov=identity
```

The tests use the memory store, the in-memory OpenFGA fake (it evaluates the packaged model) and a fast Argon2id profile (`tests/identity_support.py` has the harness). PostgreSQL tests (migrations up and down, schema drift, the store contract, a full flow) run with `MOWDECK_TEST_DATABASE_URL`; the OpenFGA integration test (Anna as Operator on group North controls North only, Bob nothing) runs with `MOWDECK_TEST_OPENFGA_URL`:

```bash
docker run -d --name identity-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test public.ecr.aws/docker/library/postgres:17.11-alpine
docker run -d --name identity-test-fga -p 18080:8080 ghcr.io/openfga/openfga:v1.22.0 run \
  --datastore-engine memory --authn-method preshared --authn-preshared-keys test-key
MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \
MOWDECK_TEST_OPENFGA_URL=http://127.0.0.1:18080 MOWDECK_TEST_OPENFGA_TOKEN=test-key \
  uv run pytest services/identity
docker rm -f identity-test-pg identity-test-fga
```

## Limits (Phase 1)

- Access tokens stay valid until they expire (at most 10 minutes) after a user is disabled; object checks in OpenFGA fail at once.
- No account switching endpoint, no invitations, no support access API, no operator console, no ownership transfer flow (the CLI `promote-owner` covers recovery), no purge job after the deletion grace period.
