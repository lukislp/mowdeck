# ADR 0021: Browser authentication flow

## Status

Accepted (2026-10-09)

## Context

[ADR 0015](0015-tokens-sessions-and-key-rotation.md) issues 10 minute access tokens and opaque refresh tokens that rotate on every use; presenting a replaced refresh token revokes the whole login session. Login and refresh already set the refresh token as an `HttpOnly`, `SameSite=Strict` cookie limited to `/api/v1/identity/session` (refreshing with the cookie requires `X-Requested-With`), but they also returned it in the JSON body, readable by any script on the page. A web app adds problems the API clients do not have: several tabs refreshing at the same moment would trip the reuse detection and log the user out, a response lost on a flaky mobile connection did the same, and Traefik's login rate limit (10 per minute per IP) also covered refresh and logout, which several devices of a household behind one address share. The WebSocket needs a fresh token before the old one expires ([ADR 0018](0018-realtime-websocket-protocol.md)).

The design ([Design: web app](../dev/design-web-app.md), sections 2 and 7) proposed the flow; this ADR records it as built in Task 10.

## Decision

**The access token lives only in memory** (module state in `apps/web/src/lib/auth/session.svelte.ts`), never in `localStorage`, `sessionStorage` or IndexedDB, and never in the service worker. A reload costs one refresh request.

**The refresh token lives only in the cookie.** The app sends `X-Mowdeck-Refresh: cookie-only` on login and refresh; Identity then sets the cookie and returns `refresh_token: null` in the body. Without the header the body still carries the token, so scripts and Home Assistant are unchanged. Refresh and logout carry `X-Requested-With`; together with `SameSite=Strict` and the narrow cookie path this is the CSRF defence (a cross-origin request with a custom header needs a preflight that the API never approves). Every other call uses `Authorization: Bearer`.

**Single-flight refresh across tabs.** The app refreshes 60 s before `exp`. The refresh runs inside the Web Lock `mowdeck-refresh`; inside the lock a tab first adopts a token that another tab obtained less than 30 s ago (BroadcastChannel `mowdeck-auth`) instead of refreshing. Every new token is broadcast, and so is logout, which returns every tab to the login page.

**One retry on 401.** The fetch wrapper retries a request once after a refresh; a second 401 or a failed refresh ends the session in the app and routes to `/login`. 403 and 404 are shown as problems and never retried.

**WebSocket re-authentication on every refresh.** The Realtime client sends `auth` with the new token on the open socket; a close with 4401 fetches a fresh token and reconnects.

**A short reuse grace in Identity** (`MOWDECK_IDENTITY_REFRESH_REUSE_GRACE_SECONDS`, default 10, 0 disables). A rotated refresh token presented again within the grace, while its successor was never used, counts as a lost response: Identity rotates the unused successor in its place (it stores only hashes, so it cannot hand out the successor again) and audits `session.refresh_grace`. Only the immediate predecessor of the newest token qualifies; anything else still revokes the family (`session.refresh_reuse`).

**Refresh and logout get their own rate limit.** Traefik's `identity-auth` router limits only login, registration and token exchange (10 per minute, burst 20); the router `identity-session` gives `/api/v1/identity/session/` (refresh, logout) 60 per minute, burst 30.

## Consequences

- Injected script can still use the session while the page is open (mitigated by the strict CSP without inline scripts other than the hashed bootstrap), but it cannot read and exfiltrate the refresh token.
- Every page load costs one refresh request; the access token is never persisted.
- Theft detection is slightly less sharp: within the grace a replayed predecessor whose successor is still unused gets a token instead of revoking the family. The window is configurable and can be turned off.
- Without the Web Locks API the in-tab single flight and the broadcast adoption remain; a lost race there ends in a new login, which is safe.
- Behind HTTPS the cookie needs the `Secure` flag (`IDENTITY_REFRESH_COOKIE_SECURE=true`); Compose development over plain HTTP keeps it off.
