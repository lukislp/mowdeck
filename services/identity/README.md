# identity

Accounts, users, roles, sessions and tokens: who is who and who may do what. Identity registers accounts, logs users in, issues the short-lived access tokens every other service verifies on its own, manages sub-users, roles and API tokens, and writes the authorization tuples of its subjects to OpenFGA.

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; layer rules in `docs/dev/service-template.md`. Design: [Design: identity and authorization](../../docs/dev/design-identity-authz.md), sections 3 and 4. Decisions: [ADR 0014](../../docs/adr/0014-authorization-model-and-tuple-ownership.md) (authorization model, tuple ownership) and [ADR 0015](../../docs/adr/0015-tokens-sessions-and-key-rotation.md) (tokens, sessions, key rotation).

## Purpose

- **Accounts** (household or business) with an owner. Registration creates the account, the owner, the default roles and the owner's tuples. Deletion locks the account for a grace period (7 days) during which the owner can restore it; the purge job is a later task.
- **Users**: persons register; **sub-users** are created by an account with a username and a password (no e-mail needed) and belong to it. Usernames are unique per installation.
- **Roles** are named bundles of permission keys per account. Owner and Admin are fixed relations (`account#owner`, `account#admin`); Manager, Operator, Viewer and custom roles are editable. A role is assigned at a level (account, site, group, mower) and Identity writes the expanded `grant_*` tuples (ADR 0014).
- **Sessions**: password login, 10 minute EdDSA access tokens, opaque refresh tokens with rotation and reuse detection; for browsers a cookie-only refresh transport and a short reuse grace after a lost response ([ADR 0021](../../docs/adr/0021-browser-auth-flow.md)).
- **API tokens** (Home Assistant) with explicit grants capped at the creator's rights, exchanged for access tokens.
- **Audit log** of every permission change and every token issue, revocation or deletion, with the correlation id.

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
- **Revoked API tokens** stay listed until the owner deletes them (there is no automatic retention). Only revoked tokens can be deleted, one (`?purge=true`) or all of the account (`purge-revoked`); each deleted token is audited as `api_token.purged` with its name, prefix, revocation time and reason (never the secret), and its subject is reconciled once more so no tuple outlives the row. Nothing references `api_tokens` by foreign key: `audit_log` keeps the token id as text and is append-only, so the token's history (`api_token.created`, `api_token.exchanged`, `api_token.revoked`) stays.
- **Guards.** The owner cannot be disabled or removed; only the owner disables or removes an admin; nobody disables or removes themselves.

### Browser sessions

The web app keeps the access token in memory and the refresh token only in the cookie `mowdeck_refresh` (`HttpOnly`, `SameSite=Strict`, `Path=/api/v1/identity/session`, `Secure` from `MOWDECK_IDENTITY_REFRESH_COOKIE_SECURE`): it sends `X-Mowdeck-Refresh: cookie-only` on login and refresh, so no response body carries the refresh token. Without the header the body keeps it (scripts, Home Assistant). Identity stores only hashes of refresh tokens, so the reuse grace cannot return the successor that was already issued; it rotates that unused successor instead and sets the new cookie. Traefik limits refresh and logout separately from login (router `identity-session`, 60 per minute, burst 30). See [ADR 0021](../../docs/adr/0021-browser-auth-flow.md).

### Permission catalogue

The catalogue holds every key of the authorization model (always present, with the levels the model allows) plus the keys of the module descriptors fetched from `MOWDECK_IDENTITY_MODULE_URLS` at startup (with retries; every 10 minutes after that, every 30 s while a module is missing) and Identity's own descriptor. Keys without a relation in the model are **account keys**: they can only be granted on the whole account and are enforced by their module from the token's `perms`. A descriptor may say a key is implied by others (`implied_by`): Device-Gateway's `device_gateway.view` is implied by `mower.view` on the whole account. Unknown keys in roles and tokens are rejected (422). Keys that disappear from a module that answered are marked retired.

### Token claims

`sub`, `acc`, `kind` (`user`, `subuser`, `api_token`), `name`, `roles`, `perms` (keys held anywhere in the account: an upper bound for pre-filters, never a grant), `plan` (asked from Entitlements at issue through the kit client with its cache; `unlimited` while billing is disabled, also when Entitlements does not answer; empty with billing enabled and no answer; informational, services decide through the client, [ADR 0028](../../docs/adr/0028-entitlements-plans-and-enforcement.md)), `sid` (login session), `jti`, `iss` (`MOWDECK_IDENTITY_ISSUER`), `aud` (`mowdeck-api`), `iat`, `nbf`, `exp` (10 minutes). Header `{"alg": "EdDSA", "kid", "typ": "at+jwt"}`.

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
| `MOWDECK_IDENTITY_TRUST_FORWARDED_FOR` | `false` | Client IP from `X-Forwarded-For` (only behind a trusted proxy; Compose sets it) |
| `MOWDECK_IDENTITY_DELETION_GRACE_DAYS` | `7` | Grace period of an account deletion |
| `MOWDECK_IDENTITY_DEFAULT_PLAN` | `unlimited` | Plan of new accounts while billing is off |
| `MOWDECK_IDENTITY_REFRESH_COOKIE_SECURE` | `true` | `Secure` flag of the refresh cookie (Compose development: `false`) |
| `MOWDECK_IDENTITY_GITHUB_LOGIN` | `false` | Feature flag of the GitHub login stub (not implemented in Phase 1) |
| `MOWDECK_IDENTITY_MAINTENANCE_SECONDS` | `60` | Key rotation, key reload, purge of login attempts older than 30 days |
| `MOWDECK_IDENTITY_RECONCILE_SECONDS` | `3600` | Full reconcile interval, 0 disables |

### Passwords

Argon2id via `argon2-cffi` 25.1.0. Default `rfc9106_low`: 64 MiB memory, 3 iterations, 4 lanes (RFC 9106 second recommendation); `owasp_min`: 19 MiB, 2 iterations, 1 lane (OWASP Password Storage Cheat Sheet minimum). 16 byte salt, 32 byte hash. Hashing runs in worker threads, at most `ARGON2_CONCURRENCY` at a time. Hashes made with other parameters are upgraded after the next successful login. Unknown usernames run a dummy verification. Policy: 10 to 256 characters, no composition rules, not on a small list of common passwords, not the username. Refresh and API token secrets are 256-bit random values stored as SHA-256 and compared in constant time.

## Authentication plug-ins

`identity.application.authenticators`: `CredentialAuthenticator` (`PasswordAuthenticator` for `POST /session`, `ApiTokenAuthenticator` for `POST /token`) and `RedirectAuthenticator` (interface for OAuth/OIDC; `GitHubAuthenticator` is a stub that raises `NotImplementedError` and is registered only with `MOWDECK_IDENTITY_GITHUB_LOGIN=true`). The `AuthenticatorRegistry` holds the enabled ones by name.

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

Tables (Alembic revision `0002`): `accounts`, `users`, `memberships` (owner and admin flags, status), `roles` (permission keys as JSON, kind), `role_assignments` (subject, role, level), `sessions` (hashed refresh tokens, family, rotation), `api_tokens` (hashed secret, grants as JSON), `signing_keys`, `audit_log` (append-only: a trigger rejects `UPDATE` and `DELETE`), `login_attempts`, `permission_catalogue`; `support_grants` and `invitations` exist as schema only.

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
