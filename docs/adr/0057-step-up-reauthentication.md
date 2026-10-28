# ADR 0057: Step-up re-authentication for critical actions

## Status

Accepted (2026-10-27)

## Context

A MowDeck session lives for days (7 days idle, 30 absolute, [ADR 0015](0015-tokens-sessions-and-key-rotation.md)): the refresh cookie keeps the browser or the app signed in. Whoever gets hold of an open browser, a phone left unlocked or a stolen session can therefore delete mowers, unlink the cloud account, mint API tokens, hand out admin roles or remove the owner's passkeys. Only some paths asked for the password again (adding a passkey, linking GitHub or Apple, changing the password, deleting the account), each with its own password field.

Wanted: a **fresh-authentication requirement** ("step-up") for the critical actions: the user must have confirmed with a passkey (preferred, user verification required) or the password within the last N minutes (default 5, configurable). Every service that owns a critical action must be able to check it without calling Identity, the web app must handle it generically, and API tokens (Home Assistant) are not interactive.

## Decision

**Claims.** The access token carries `auth_time` (epoch seconds of the user's last interactive authentication) and `amr` (how: `pwd`, `passkey`, `github`, `apple`), the OpenID Connect claims of the same names. `Principal` has `auth_time` and `auth_methods`; API tokens and services have neither.

**Where the time lives.** On the login session: `sessions.auth_time` and `sessions.auth_method` (migration 0005). A login sets them to now (password, passkey, GitHub, Apple alike: a provider login is a real, interactive authentication). A refresh copies them to the successor, so a 10 minute token that is renewed keeps the time of the last real authentication and the window really ends. A native app hand-off carries over the time and method of the browser login that is handed over. Sessions from before the migration have no time: they count as stale, the first critical action asks once.

**Step-up endpoints** (Identity, signed-in users with a login session; API tokens get 403):

- `POST /me/step-up/password` `{password}`: the password check of the existing `Reauthenticator` (login limit per address, the per-user failure throttle, a wrong password counts as a failed login and is audited as `step_up.reauth_failed`).
- `POST /me/step-up/passkey/options` and `POST /me/step-up/passkey`: the passkey ceremony of the login (user verification required, single-use challenge, looser limit for the options and the login limit for the answer). The assertion must come from a passkey **of the caller**; another user's passkey fails (`step_up.failed`).
- Success stamps the live session of the login session with now and the method, issues **a new access token** (same session, current claims, `auth_time` = now, 10 minute lifetime) and writes `step_up.succeeded`. No refresh token and no cookie are involved; the response is `TokenOut`.

**Check in the services.** `mowdeck_auth.require_recent_auth(max_age=None)` is a FastAPI dependency (and `ensure_recent_auth(principal, ...)` a function for use cases) in `mowdeck-auth`. Declared after the permission dependency, so a caller without the right still gets 403/404 first. Fresh: the request goes on. Missing or too old (or a future time more than a minute ahead): **401** `urn:mowdeck:problem:reauth-required` with the RFC 9470 challenge `WWW-Authenticate: Bearer error="insufficient_user_authentication", max_age="300"`. Window: `MOWDECK_AUTH_STEP_UP_MAX_AGE_SECONDS`, default 300, clamped to 30..3600, read by every service from its own environment (one value in Compose or Helm).

**API tokens and services** can never confirm: `require_recent_auth` answers **403** `urn:mowdeck:problem:step-up-unavailable` ("this action needs an interactive sign-in"). Deliberately conservative; there is no scope that waives it. A token that needs one of these actions is the wrong tool: do it in the web app. The Home Assistant integration uses none of the protected actions (checked: commands, firmware check, camera, telemetry, settings reads and the like only).

**Protected actions** (what was verified to exist, and where):

| Action | Endpoint | Service |
| --- | --- | --- |
| Delete a mower | `DELETE /fleet/mowers/{id}` | Fleet |
| Remove a linked Mammotion cloud account | `DELETE /fleet/linked-accounts/{id}` | Fleet |
| Create an API token (its grants are fixed at creation: changing scopes means a new token) | `POST /identity/accounts/{id}/api-tokens` | Identity |
| Delete the account | `DELETE /identity/accounts/{id}` | Identity |
| Create a sub-user, remove a user or sub-user, set a sub-user's password | `POST .../users`, `DELETE .../users/{id}`, `POST .../users/{id}/password` | Identity |
| Change roles and admin rights | `POST/DELETE .../role-assignments`, `PATCH .../roles/{id}` | Identity |
| Remove a passkey (own, or a sub-user's as admin) | `DELETE /me/passkeys/{id}`, `DELETE .../users/{id}/passkeys/{id}` | Identity |
| Unlink GitHub or Apple | `DELETE /me/external-identities/{provider}` | Identity |
| Add a passkey, link GitHub or Apple | `POST /me/passkeys/options`, `POST /me/external-identities/{p}/link` | Identity |
| Delete the account (owner) | (above) | Identity |

*Unified with the older password re-checks:* adding a passkey, linking a provider and deleting the account keep the optional `password` field (existing clients and tests); **without it** the request needs a recent step-up. The web app uses the one sheet instead of three password fields.

*Not protected, with reasons.* **Change password** already needs the current password in the request: that is a confirmation at least as strong as a step-up. **Firmware update** (`firmware_update` is a mower command): it is vendor-signed, the mower recovers from a failed update, and it is an ordinary command that automations (Home Assistant) may legitimately send; stopping it would break those and the generic command channel, for little gain. **Theft alarm settings** (notification preferences and geofences): personal, reversible, and turning off an alert deletes no data and grants no access; a later task can protect geofence removal if wanted. **Disabling a user, revoking or purging an API token, logging out everywhere**: they only reduce access, and in an incident they must stay quick. **Mower settings, commands, schedules, camera**: operational, not account security.

**Web app.** The API client recognises the 401 `reauth-required` before its refresh logic (the session is fine, a refresh would not help and a second 401 must not end the session). It asks the `StepUp` controller, which opens **one** re-authentication sheet (passkey button when the browser and the installation support it, password field always), calls the step-up endpoint with a plain `fetch` (a wrong password is a 401 too and must not run through the retrying client), hands the new token to the session (and to the other tabs through the existing BroadcastChannel), and the client repeats the request once. Dismissing the sheet returns the original problem to the caller.

**Native apps.** Passkeys cannot run inside the Capacitor web view ([ADR 0050](0050-passkeys-and-app-login-hand-off.md)). The sheet therefore offers only the password there, with a short note; every user has a password (it always stays). A step-up confirmation through the system browser (a hand-off like the login) would give passkey users the same convenience in the app; it needs a new short-lived server-side request and one-time code and is left as a follow-up, since the password path is complete and secure. The session of an app that signed in through the hand-off carries the browser login's `auth_time`, so the first critical action right after signing in does not ask again.

**Audit and limits.** `step_up.succeeded` (method, session, token id, passkey id), `step_up.failed` (passkey), `step_up.reauth_failed` (password; also counted in `login_attempts`, so the password login throttle applies to guessing here too). The address limit of the login applies to the password and passkey answers; the options use the looser challenge limit.

## Consequences

- One mechanism for every service: a new critical endpoint adds `Annotated[Principal, Depends(require_recent_auth())]` after its permission check. Services need no call to Identity and no new secret.
- Clock: services compare `auth_time` with their own clock. An `auth_time` more than a minute in the future counts as stale, so clock skew shows as an extra prompt, never as a hole.
- A token stays valid for 10 minutes, but a stolen access token alone never carries a fresh `auth_time` beyond the 5 minute window of the real user's last confirmation; a stolen session (refresh cookie) must prove a passkey or the password.
- Roles and permissions in the step-up token are re-read from the database like on a refresh.
- Deleting the account keeps working with a password in the body; a client that sends none gets the sheet.
- A rejected idea: a separate short-lived "step-up token" sent in a second header. It would need its own verification, its own header handling in every client and would not survive the page reload as simply; the claim in the normal token is checked by the same verifier everywhere.
