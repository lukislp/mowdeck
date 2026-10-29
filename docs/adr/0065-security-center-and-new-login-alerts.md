# ADR 0065: Security center and new-login alerts

## Status

Accepted (2026-10-29)

## Context

Identity already manages everything that protects an account, but each piece lives somewhere else: passkeys and linked login providers in the profile ([ADR 0050](0050-passkeys-and-app-login-hand-off.md), [ADR 0051](0051-sign-in-with-github.md), [ADR 0052](0052-sign-in-with-apple.md)), personal access tokens under API access ([ADR 0064](0064-personal-access-tokens-and-public-api-docs.md)), step-up confirmations ([ADR 0057](0057-step-up-reauthentication.md)) and the audit log for admins. Sessions with refresh rotation exist ([ADR 0015](0015-tokens-sessions-and-key-rotation.md), [ADR 0021](0021-browser-auth-flow.md)), but a user cannot see where they are signed in, cannot end one session (only "log out everywhere") and is not told when somebody signs in from a device they never used.

Forces:

- Users only ever see their own data here; an admin must not read another user's sessions through this page.
- Access tokens are EdDSA JWTs every service verifies offline for 10 minutes. A revocation list in every service is a larger change than this feature.
- No third-party services: there is no IP geolocation, and a home installation often sees only private addresses.
- An attacker with a stolen session must not be able to throw the owner out or silence the alerts.
- The alert must reach the user through what they already set up (inbox, Web Push, APNs, ntfy), and only that user: Home Assistant tokens receive account-wide notifications.

## Decision

### Sessions and devices

A login session is a refresh token family (`sessions.family_id`, the `sid` claim). `GET /me/sessions` lists the caller's live families in every account they belong to: the newest row gives the device (from its user agent), the address and the last use, the first row the start and the login method. The current session (the `sid` of the request) comes first and is marked.

The device is described from the user agent only: client kind (`browser`, `app` from the `MowDeckApp` token the native shells append, `script`, `unknown`), browser family and operating system, never versions (an update is not a new device). Addresses are shortened to their network (`203.0.113.x`, an IPv6 `/48`) and private, loopback and link-local addresses are called the home network. No location service is asked.

### Revocation and its window

`DELETE /me/sessions/{id}` and `POST /me/sessions/revoke-others` mark every row of the family revoked in one transaction:

- the refresh token fails at once (the next rotation sees `revoked_at`);
- **Identity's own endpoints** refuse an access token whose session has ended at once: the request context checks the family of the `sid` (one indexed query; a session Identity does not know, such as a test token, is not refused). This covers every account function: sessions, passkeys, tokens, users, roles;
- **every other service** keeps accepting the access token until it expires, at most `MOWDECK_IDENTITY_ACCESS_TOKEN_MINUTES` (default 10, at most 15). The web app of the revoked device refreshes before that and is signed out; open realtime sockets close when their token expires.

A per-service revocation list (an `identity.session.revoked` event, a deny cache in `mowdeck-auth`) would close that window and is left for later: 10 minutes is the bound the token design already accepts for disabled users.

Both actions need a recent step-up (ADR 0057): a stolen session cannot end the owner's sessions, and the owner can always confirm with the password or a passkey. Revoking the current session is allowed (it is a logout). Unknown or foreign session ids answer 404, an already ended own session 204.

### New-login alerts

Every login records its device in `known_devices`: a random id in the HttpOnly cookie `mowdeck_device` (`SameSite=Lax` so it comes along on a provider's redirect back, path `/api/v1/identity`, 400 days), of which only the SHA-256 is stored per user. A login from a device without a row of the user publishes `identity.session.new_device` (`NewDeviceLogin`: user, account, session, device parts, address prefix, method) when

- the user has new-login alerts on (`users.new_login_alerts`, on by default),
- the user already had another known device: the very first login, and the first one after this change, record the device silently, and
- the login is not the native app taking over a browser login (`app_handoff`): the browser login before it was checked a moment earlier.

Notifications turns it into `security_new_login` (severity `warning`) for that one user: an inbox item and deliveries over the user's channels of that account (saved preferences or the defaults), also during quiet hours, deduplicated per session, linking to `/settings/security?revoke=<session>`. It is never published as `notifications.raised` (account-wide, Home Assistant) and is not one of the per-kind preferences: the opt-out is the switch on the security page, and turning it off needs a step-up.

A cleared cookie or a private window is a new device again. That is accepted: no fingerprinting, and an alert too many is better than one too few.

### The security page and the rest

`GET /me/security` returns the password status (set, last change from the new `users.password_changed_at`, must change), the alert setting and the counts of passkeys, linked providers, active personal tokens, live sessions and known devices. `GET /me/security-events?days=30` returns the caller's own audit rows of the period (logins and failed logins, new devices, ended sessions, passkeys, linked providers, password changes, personal and widget tokens, failed confirmations), each once although some actions are audited per account, with only a safe subset of the details and the address shortened. Two indexes on `audit_log` (`target_id, at` and `actor_subject, at`) serve it.

Unlinking a login provider is refused (409) when it would leave no way in (no password, no passkey, no other provider). Users always have a password today; the guard keeps it that way if that changes.

The web app gets a **Sicherheit** section right after the profile: five gauges (password, passkeys, sign-in services, personal tokens, devices), the device list with "sign out" and "sign out everywhere else", the alert switch, the passkeys and login providers (moved from the profile), the active personal tokens with an inline revoke, and the events as a timeline. The provider callback of a link now returns to `/settings#security`.

Migration `0008` adds `known_devices`, `users.password_changed_at`, `users.new_login_alerts` and the two audit indexes.

## Consequences

- Users can see and end their sessions, and learn about sign-ins they did not make, without an admin.
- Revocation is immediate for refresh tokens and Identity, bounded by 10 minutes elsewhere; the page says so.
- Every login writes or touches one `known_devices` row; the table grows with devices, not with logins. Rows go with the user.
- The device cookie is one more cookie under `/api/v1/identity`; it carries no secret beyond the device id and is useless without the user's credentials.
- Existing users get no alert until their second device after the update.
- The end-to-end tests that register through the API and then sign in in the browser turn the alerts off in their shared helper (two devices); the security test turns them back on.
- Follow-ups: a revocation event for the other services, an "unknown last change" password date for existing users, and optional rate limiting of alerts for users who clear cookies on every visit.
