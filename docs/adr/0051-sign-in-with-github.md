# ADR 0051: Sign in with GitHub

## Status

Accepted (2026-10-25)

## Context

[ADR 0002](0002-self-built-identity-service.md) made login methods plug-ins behind `CredentialAuthenticator` and `RedirectAuthenticator`; Phase 1 left a disabled `GitHubAuthenticator` stub and a draft in the identity design (section 3.3). [ADR 0015](0015-tokens-sessions-and-key-rotation.md) defines the sessions every login must end in, [ADR 0021](0021-browser-auth-flow.md) how the browser holds them (access token in memory, refresh token only as an `HttpOnly`, `SameSite=Strict` cookie). [ADR 0050](0050-passkeys-and-app-login-hand-off.md) built the native app hand-off login-method agnostic, explicitly for a redirect login to reuse.

Owner decisions:

1. **GitHub only for now.** A generic OIDC provider comes later, in its own pull request; the code must let an OIDC authenticator slot in.
2. **Only explicitly linked accounts.** A signed-in user links their GitHub account in their settings; a GitHub account that is not linked is refused at sign-in, there is no registration through GitHub, and accounts are never matched or linked by e-mail address.
3. **Server-side configuration only:** environment variables and Helm values, the client secret as a secret file (Docker secret, Kubernetes Secret). Not in the database.
4. **No GitHub configuration in the web app,** not even for administrators. The only surfaces: "Mit GitHub verknüpfen" / "Verknüpfung lösen" (with the linked GitHub login) in the user's own settings, and "Mit GitHub anmelden" on the sign-in page when GitHub is configured (`GET /login-methods` exposes a boolean). Status and misconfiguration go to the Identity log at startup and to the operations docs.
5. **The password always stays;** GitHub never replaces it, and unlinking is always allowed.

Forces:

- An OAuth callback is a top-level navigation from github.com. Our session cookie is `SameSite=Strict` and the access token lives in page memory, so the callback cannot see who is signed in; whatever binds the round trip to the browser and to the user must travel another way.
- GitHub OAuth Apps support PKCE (S256, since 2025-07) but still require the client secret. The `email` of `GET /user` is whatever the user set public, and e-mail addresses can be changed or recycled; GitHub's numeric user id never changes, the login name can.
- `state` alone prevents login CSRF only if it is bound to the browser that started the flow.
- The access token GitHub issues for an OAuth App does not expire by itself.

## Decision

**Plug-ins.** The `RedirectAuthenticator` protocol becomes `start(purpose, user_id?, return_query?) -> RedirectStart` (the provider URL and a secret the browser keeps) and `callback(params, browser_secret) -> RedirectCallback` (purpose, user, provider profile, or a failure code; it never raises for input). `OAuthAuthenticator` implements it for any OAuth 2 provider on top of the port `OAuthClient` (`authorization_url(...)`, `profile(code, code_verifier, redirect_uri) -> ExternalProfile{subject, display_name}`); `GitHubAuthenticator` is that class with the GitHub client (`adapters/github.py`, httpx). An OIDC provider later is a second `OAuthClient` (discovery, ID token validation) behind the same authenticator, registered the same way.

**Round trip.** Authorization code flow with PKCE S256, scope `read:user`, `allow_signup=false`. `start` stores a single-use row in `oauth_states`: the SHA-256 of a random 256-bit `state`, the S256 hash of a random 384-bit PKCE verifier, the purpose (`login` or `link`), the user for a link, and for a login the sign-in page's query to return to; valid 10 minutes, purged by the maintenance loop. The verifier itself goes to the browser as the cookie `mowdeck_oauth` (`HttpOnly`, `SameSite=Lax` so it comes along on GitHub's top-level redirect, path `/api/v1/identity/oauth/`, `Secure` with the refresh cookie's setting, 10 minutes). The callback deletes the row whatever happens, requires the cookie's verifier to match the stored hash (another browser, a replay, an expired or unknown state: `expired`), sends code and verifier to GitHub, reads `GET /user`, keeps only `id` and `login`, and revokes the GitHub token right away (`DELETE /applications/{client_id}/token`, best effort): MowDeck never stores a GitHub token. Neither a stolen database row nor a stolen callback URL finishes a round trip.

**Identity of a GitHub account:** GitHub's numeric user id (`subject`), the login name only for display (updated at each sign-in). E-mail addresses are neither requested nor read.

**Links** live in `external_identities` (migration 0004): `user_id` (cascade from `users`), `provider`, `subject`, `display_name`, `linked_at`, `last_used_at`, unique `(provider, subject)` (a GitHub account belongs to one user) and `(user_id, provider)` (a user has one GitHub account). Linking: `POST /me/external-identities/github/link {password}` re-checks the password exactly like adding a passkey (the shared `Reauthenticator`: per-address login limit, per-username failure throttle, a wrong password counts as a failed login and is audited `external_identity.reauth_failed`), refuses a second link (409) and starts a `link` round trip for the caller. The callback links the account to the user who started it; an account linked to another user ends with `taken`, a different account already linked with `already`, a disabled user with `denied`. `GET /me/external-identities` lists the caller's links (provider, GitHub login, linked, last used); `DELETE /me/external-identities/github` unlinks without a password (it only removes a way in), also after GitHub was turned off. Only users have links (API tokens: 403). Admins see no links of others; no admin surface exists.

**Login.** `POST /oauth/github/start {return_query}` hands out the authorization URL (it checks nothing: the looser limit of a passkey challenge); the callback finds the user only through `external_identities` and starts exactly the session of a password login (`_start_session`: same claims, refresh token rotation, the refresh cookie set on the callback's redirect, `session.login` with `method: github`, the link's id and the GitHub id). Unknown accounts, disabled users and users without a usable membership end with the same `denied`; nothing tells a stranger whether a MowDeck user exists (the result only tells the holder of that GitHub account whether it is linked). Failures are audited as `session.login_failed` (method, reason) without a `login_attempts` row, like passkeys: there is nothing to guess, and failures must not lock the password login.

**Redirects back to the web app** are fixed local paths, never a target from the request: `/login?<query>&signed_in_with=github` or `&sign_in_error=<code>&provider=github` (codes `cancelled`, `expired`, `denied`, `unavailable`, `rate_limited`), `/settings?linked=github#profile` or `?link_error=<code>&provider=github#profile` (plus `taken`, `already`). The login's `<query>` is reduced at the start to `next` (a local path; `//host`, backslashes and schemes are dropped), `app_request` (a UUID) and `app_state`. The callback answers `303` with `Cache-Control: no-store` and `Referrer-Policy: no-referrer` and drops the binding cookie.

**Native apps.** No app change: in the app the GitHub button runs the existing system-browser hand-off of ADR 0050 (`/login?app_request=…&app_state=…` in `ASWebAuthenticationSession` or a Custom Tab), where the user picks "Mit GitHub anmelden". The query survives GitHub's round trip; back on the sign-in page with a fresh session the page asks "In der App weitermachen?" and calls `POST /app-login/code` exactly as after a passkey login. Linking happens in a browser (the web view cannot finish a round trip that leaves it); unlinking works in the app too.

**Configuration** (server side only): `MOWDECK_IDENTITY_GITHUB_CLIENT_ID`, the secret as `MOWDECK_IDENTITY_GITHUB_CLIENT_SECRET_FILE` (any setting can be a file through the service kit), and `MOWDECK_IDENTITY_PUBLIC_URL`, from which the callback URL `<public URL>/api/v1/identity/oauth/github/callback` is built (HTTPS; plain HTTP only for localhost and loopback). The GitHub endpoints are settings with github.com defaults (GitHub Enterprise Server, the test stack's fake). GitHub is on only when id, secret and a usable public URL are set; otherwise Identity logs why at startup (`GitHub login is off: …`) and keeps running; when on it logs the callback URL to register. Compose mounts `infra/compose/secrets/github/` (git-ignored) at `/run/secrets/github`; Helm takes `services.identity.github.{enabled, clientId, existingSecret, secretKey}` and mounts the operator's Secret as a file, like the APNs key. The previous flag `MOWDECK_IDENTITY_GITHUB_LOGIN` is gone.

**Rate limits.** Traefik's `identity-auth` router (and Helm's `-auth` Ingress) covers the callback and the link start; `POST /oauth/github/start` joins the passkey challenge router (60 per minute, burst 30). Identity applies the same limits in-process; a limited callback ends with `rate_limited`.

Options considered:

- **Authlib's Starlette client** (the Phase 1 draft): state and verifier in a signed session cookie. Rejected: one more dependency, a session middleware for one flow, and its default stores the state where the server cannot make it single use. httpx is already a dependency; the flow is three requests.
- **A signed state cookie only, no table:** cannot be made single use without server state, and a replayed callback within its lifetime would start a second session.
- **Setting a one-time code in the redirect for the page to exchange** (as for the app): an extra bearer value in a URL; the callback can set the refresh cookie itself (a cookie set on a top-level navigation's response is stored, `SameSite` restricts only sending) and the page restores the session as on every load.
- **Matching by verified e-mail address** or **registration through GitHub:** refused by the owner; an e-mail address is not a stable, exclusive identifier.
- **An admin page with the GitHub status or callback URL:** refused by the owner; the log and the operations docs carry it.
- **The `user:email` scope:** unnecessary without e-mail matching.

## Consequences

- GitHub sign-in works only at the installation's public address (the callback URL registered in the GitHub OAuth App); a second address of the same server needs its own OAuth App. GitHub never calls the server: the user's browser carries the redirect, so a LAN-only installation works as long as Identity can reach github.com and api.github.com.
- Two browser tabs starting a GitHub round trip at the same moment share one binding cookie: the earlier one ends with `expired` and is simply started again.
- A user removed from MowDeck loses the link (cascade); a GitHub account deleted at GitHub leaves a link nobody can use until the user unlinks it.
- In the app, signing in with GitHub needs the app build with the hand-off (`MowDeckShell.openAuthSession`, ADR 0050); on iOS the authentication session is ephemeral, so the user signs in to GitHub there each time (no shared Safari cookies). If the GitHub app is installed on Android, a Custom Tab may offer to open github.com links in it; the OAuth page itself stays in the tab (inferred, to be checked on a device).
- Verified with unit and API tests against a strict fake GitHub (httpx mock transport, PKCE and client secret checked), PostgreSQL migration up and down plus the constraints and the cascade, web unit tests, and a Playwright test against the test stack's fake GitHub container (link, sign in, unlink, refused afterwards). Not verified: the real github.com OAuth App (the owner registers it), the native apps on a device.
