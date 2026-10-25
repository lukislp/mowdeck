# ADR 0015: Tokens, sessions and key rotation

## Status

Accepted (2026-10-09)

## Context

[ADR 0002](0002-self-built-identity-service.md) chose a self-built Identity service. The plan asks for short-lived signed tokens (JWT, 5 to 15 minutes) that every service verifies itself with a public key, without a call to Identity per request; for Argon2 passwords, a login rate limit, short sessions and API tokens per client (one for Home Assistant). Services already turn a token into a `Principal` (`mowdeck_contracts.v1.principal`) and check object rights in OpenFGA ([ADR 0014](0014-authorization-model-and-tuple-ownership.md)).

Open points for the first implementation:

- Which algorithm and lifetime, and what goes into the token?
- How are sessions kept alive without long-lived access tokens, and how is a stolen refresh token noticed?
- How do API tokens (long-lived, chosen by a user) fit the "services only verify JWTs" rule?
- How do signing keys rotate without breaking verification in other services?

The full design is in [Design: identity and authorization](../dev/design-identity-authz.md), sections 3 and 4.

## Decision

**EdDSA access tokens, 10 minutes.** Identity signs JWTs with Ed25519 (`alg: EdDSA`, RFC 8037), header `typ: at+jwt` (RFC 9068) and a `kid`. Claims: `sub`, `acc` (account), `kind` (`user`, `subuser`, `api_token`), `name`, `roles`, `perms`, `plan`, `sid`, `jti`, `iss`, `aud`, `iat`, `nbf`, `exp`. `perms` is the union of permission keys the subject holds anywhere in the account: an upper bound that services may use as a pre-filter (deny without asking OpenFGA), never as a grant for an object. Object rights always come from OpenFGA. Tokens stay well under 1 KB. PyJWT 2.15.1 does not implement the fully specified `Ed25519` algorithm name of RFC 9864, so `EdDSA` is the only accepted value for now; switching later only changes the signer and the allow list.

**Services verify locally.** The library `mowdeck-auth` fetches Identity's JWKS (`http://identity:8000/.well-known/jwks.json` inside Compose), caches it for 5 minutes, refreshes once when a token names an unknown `kid` (rate limited to one fetch per 30 s), serves the last good set for at most an hour while Identity is unreachable, and accepts only `EdDSA` with OKP keys (no `none`, no HMAC). `iss`, `aud`, `exp`, `nbf` and `iat` are checked with a 30 s leeway. Every failure answers 401 with one generic message; the reason is logged.

**Opaque, rotated refresh tokens with reuse detection.** A login returns a 256-bit random refresh token (stored as SHA-256 only) with a 7 day idle and a 30 day absolute lifetime. Every refresh replaces it and issues a new access token with claims read fresh from the database. Presenting a replaced refresh token again revokes the whole login session (`family_id`, which is also the JWT `sid`) and is audited. The web app gets the refresh token additionally as an `HttpOnly`, `SameSite=Strict` cookie limited to `/api/v1/identity/session`; refreshing with the cookie requires an `X-Requested-With` header.

**API tokens are exchanged for JWTs.** An API token (`mdk_<id>_<secret>`, SHA-256 at rest, compared in constant time) is its own OpenFGA subject with explicit grants capped at its creator's rights. A client exchanges it at `POST /api/v1/identity/token` for a 10 minute access token with `kind=api_token` and no refresh token. So services only ever verify JWTs; a revoked or expired API token stops working with the next exchange, and its tuples are removed at once.

**Signing keys rotate with an announced next key.** Keys are stored in Identity's database, private halves encrypted with AES-256-GCM under a key-encryption key from `MOWDECK_IDENTITY_KEY_ENCRYPTION_KEY` (or `_FILE`). Statuses: `next` (published, not signing), `active` (signing), `retiring` (published until its tokens expired), `retired`. A maintenance loop announces a next key when the active one is older than `MOWDECK_IDENTITY_KEY_ROTATION_DAYS` (default 30), promotes it after an hour (well beyond the 5 minute JWKS cache) and retires the old key an hour later (token lifetime plus skew). The JWKS therefore shows at most the active key, one retiring key and the announced next key, with `Cache-Control: max-age=300`. The CLI `python -m identity.cli rotate-keys --now` activates a new key immediately and `retire-key` drops one at once (emergency); tokens signed with a retired key fail as soon as the services refresh their JWKS (immediately for an unknown kid, at most 5 minutes otherwise).

**Passwords and throttling.** Argon2id with the RFC 9106 low-memory profile (64 MiB, t=3, p=4; `owasp_min` with 19 MiB, t=2, p=1 for small hosts), at most four hashes at a time, hashes upgraded after login when the parameters change, a dummy verification for unknown users. Logins and token exchanges are limited per client IP (in-process token bucket, plus Traefik's `rateLimit` in Compose) and per username (failures within 15 minutes in the database, 429 after 10).

## Consequences

- No service calls Identity per request; Identity being down blocks logins and refreshes, not API calls with valid tokens.
- A disabled user or a removed grant loses object rights at once (tuples are removed, and `identity.grants.changed` clears the services' decision caches), but a token-level check such as `require_permission` can accept the old access token until it expires (at most 10 minutes). Revocation lists are deliberately not used.
- New rights reach the token-level pre-filter with the next access token; the web app refreshes after changing roles.
- Operators must keep the key-encryption key safe and backed up: without it Identity cannot sign, and all sessions must start over after a new key is created.
- Multi-account switching (`/session/switch-account`) and OIDC login are later work. Passkeys came with [ADR 0050](0050-passkeys-and-app-login-hand-off.md), GitHub login (the first `RedirectAuthenticator`) with [ADR 0051](0051-sign-in-with-github.md), Apple login with [ADR 0052](0052-sign-in-with-apple.md).

## Addendum: deleting revoked API tokens (2026-10-24)

Revoking kept the row, and nothing ever removed it: an account that rotates its Home Assistant token collected dead entries, which the owner had to delete in the database by hand.

- **Lifecycle.** Active, then revoked (by the owner, by disabling or removing the creator, by an account deletion request), then optionally deleted. Deleting is allowed only for revoked tokens; an active token must be revoked first, so the `identity.api_token.revoked` event that services rely on always goes out first.
- **API.** `DELETE /accounts/{id}/api-tokens/{token_id}?purge=true` deletes one revoked token (204; 409 while it is not revoked; 404 for unknown and foreign tokens). `POST /accounts/{id}/api-tokens/purge-revoked` deletes all revoked tokens of the account and answers `{deleted, ids}`. Both need `can_manage_users`, like revoking. The query flag keeps one resource with one verb instead of a second path per token; the bulk call is an action on the collection like `/reconcile`.
- **History.** Each deleted token is audited as `api_token.purged` (name, prefix, revocation time and reason, `bulk`; never the secret or its hash). No table has a foreign key to `api_tokens`; `audit_log` refers to the token by id as text and is append-only, so the earlier rows of the token stay. The token's subject is reconciled in the same unit of work, so no OpenFGA tuple can outlive the row (the account reconcile would no longer see it).
- **No retention.** Revoked tokens are not deleted automatically; deleting stays a manual decision of the owner.
