# ADR 0064: Personal access tokens and the public API documentation

## Status

Accepted (2026-10-28)

## Context

The owner wants a public API documentation with tokens: users write their own scripts against MowDeck (a rain sensor that pauses the mowers, a garden display, a daily report) with clear documentation and examples.

What exists:

- **Account API tokens** ([ADR 0015](0015-tokens-sessions-and-key-rotation.md), [ADR 0025](0025-home-assistant-integration-as-api-client.md)): created by a user with `can_manage_users` under `/accounts/{id}/api-tokens`, with any grant the creator holds (also `mower.drive`, `fleet.manage`), optional expiry, secret shown once and stored as SHA-256, exchanged at `POST /identity/token` for 10 minute access tokens, last use recorded. Made for Home Assistant: an admin connects the household.
- **Widget credentials** ([ADR 0059](0059-ios-widgets-and-live-activity.md)): any user creates a narrow API token for themselves (`mower.view`, `mower.control`, 90 days) under `/me/widget-credentials`.
- **Step-up** ([ADR 0057](0057-step-up-reauthentication.md)) for critical actions; API tokens can never pass it.
- OpenAPI 3.1 documents of every service in `apps/web/openapi` (exported by `uv run poe openapi` for the generated TypeScript types), and an MkDocs site (strict build in CI) mirrored to the GitHub wiki.

Forces:

- A household member without admin rights should still be able to script against what they may see and do, without asking the owner for an admin token.
- A token for a script lives in a file or a cron job: it should hold little, always expire and be easy to revoke, and it must never be able to create or hide other tokens.
- The documentation must match the code of the version a user runs, work offline and in the wiki mirror, and load nothing from a CDN at runtime.

## Decision

### Personal access tokens are API tokens with `purpose = personal`

No second token mechanism: a personal access token is a row of `api_tokens` with the new column `purpose` (`integration` for every existing token, `personal` for these; migration `0006`). It is its own subject `api_token:<id>` in OpenFGA with `grant_*` tuples, exchanged at `POST /identity/token` like every API token, and revoked with its creator (disable, removal, account deletion). Everything that already holds for API tokens holds here: the secret (`mdk_<id>_<random>`) is shown once and only its SHA-256 is stored; the exchange records `last_used_at` (at most every few minutes) and is audited.

The rules that make it *personal*:

| Rule | Value |
| --- | --- |
| Who creates | Any user or sub-user, for themselves (`POST /api/v1/identity/me/api-tokens`), no `can_manage_users` needed |
| Scopes | Curated: `mower.view` (read state, telemetry, history, map, realtime through the implied account keys), `mower.control` (start, pause, resume, dock), `schedule.edit`. `mower.view` is required with any other. Never `mower.drive`, `fleet.manage`, `notifications.manage`, or any key a token cannot hold anyway (`users.manage`, `billing.manage`, `audit.view`) |
| Rights | Capped at the creator's on the level (default the whole account; a site, group or mower is accepted): a viewer gets a read-only token, a request beyond one's rights is 403 |
| Expiry | Required, `expires_in_days` 1 to 365; the UI offers 30, 90, 180 days and one year |
| Step-up | Creating needs a recent confirmation (`FreshAuth`, 401 `reauth-required`); API tokens get 403 there |
| Self-management | `GET`, `POST /me/api-tokens` and `DELETE /me/api-tokens/{id}` answer 403 to every API token, so a token never sees, creates or revokes tokens. Revoking an own token needs no step-up (it only reduces access) |
| Limit | 25 active personal tokens per user (409 above) |
| Oversight | Personal tokens are listed in the account's token list with `purpose`, so admins see and can revoke them |
| Audit | `api_token.personal_created`, `api_token.revoked`, `api_token.exchanged` |

Widget credentials keep their own endpoint and stay `integration` tokens recognised by their name prefix; they are a device credential, not a token a person handles.

Considered: a separate `personal_tokens` table and a direct Bearer use of the long-lived secret on every service. Rejected: every service already verifies only Identity's short EdDSA access tokens; a second credential type would mean a second verification path in every service, a revocation check per request and no 10 minute bound on a leaked secret. The exchange costs a script one request per 10 minutes.

### Web UI: "API-Zugang" in the settings

A settings section for every user (module `api-access`, no permission needed, after the account's API tokens). It shows the base URL of the installation (`<origin>/api/v1`), links to the scripting guide and the API reference, the active tokens with scope chips, a lifetime bar and the last use, revoked and expired tokens folded away, and a form with the three scopes as tiles (read locked on while another is chosen), the lifetime as a segmented choice and a note that a confirmation follows. After creation the secret is shown once with a copy button and a quick start (curl with `MOWDECK_TOKEN` from the environment, never with the secret in it). German first, English messages complete.

### Documentation: a hand-written guide and a generated reference

- **Guide** `docs/guides/scripting.md`: creating a token, the exchange, base URL, rate limits, the problem+json error format, and examples in curl, Python (httpx) and JavaScript (`fetch`) for listing mowers, reading the state, start/pause/dock with the command status, today's history and the realtime WebSocket (which accepts the access tokens of personal tokens like any other).
- **Reference** `docs/reference/api/<service>.md` plus an index, **rendered as plain Markdown from the OpenAPI documents** by `tools/repo-checks/scripts/api_reference.py` (`uv run poe api-reference`). The CI docs job runs `api-reference-check` (stale pages fail like a stale OpenAPI export or architecture page), and a repository test checks every service has a page in the MkDocs navigation.

Considered for the reference: Swagger UI, Redoc or Scalar as a static page, and MkDocs plugins (`mkdocs-render-swagger-plugin`, `neoteroi-mkdocs`). The JavaScript viewers either load from a CDN or need a pinned bundle checked in or built in CI, do not render in the wiki mirror or on GitHub, and add a dependency to keep current; the plugins add a build dependency and render nothing outside MkDocs. Plain generated Markdown has no runtime, works on every surface, diffs well in review, and the raw OpenAPI documents stay one link away for anyone who wants an interactive viewer or a client generator.

## Consequences

- Every user can script against MowDeck within their own rights, without an admin and without a credential that outlives a year.
- A leaked personal token is limited to reading and basic control, for at most its lifetime and, after a revoke, for at most the 10 minutes of an access token already issued.
- The reference is always the code's: a change to an endpoint changes the OpenAPI export, the TypeScript types, the architecture page and the reference in the same pull request.
- The reference pages are large (fleet and telemetry about 100 KB of Markdown); the site search handles them, and the wiki mirror splits nothing.
- The step-up is needed once per token: a script that creates its own tokens has to run with a user session that recently confirmed, which is the point.
- Follow-up: Swagger-style "try it out" in the web app, if users ask for it, would reuse the same OpenAPI documents; a gateway rate limit for the API beyond the login routes is still open and documented as such in the guide.
