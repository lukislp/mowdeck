# ADR 0050: Passkeys and the app login hand-off

## Status

Accepted (2026-10-25)

## Context

[ADR 0002](0002-self-built-identity-service.md) made login methods plug-ins behind `CredentialAuthenticator` and `RedirectAuthenticator`; [ADR 0015](0015-tokens-sessions-and-key-rotation.md) listed passkeys as later work. The owner wants to sign in with passkeys (Face ID, Touch ID, Windows Hello, a phone) in the browser and in the iOS and Android apps.

Owner decisions:

1. Passkeys replace the password at login: passwordless, discoverable credentials, a "Mit Passkey anmelden" button without a username, plus the autofill offer (conditional UI) where the browser has it. Not a second factor.
2. The password always stays as the fallback; an account is never passkey-only, so there are no recovery codes.
3. The native apps ([ADR 0036](0036-native-app-shells-with-capacitor.md)) sign in with a passkey through the system browser (iOS `ASWebAuthenticationSession`, Android Custom Tab) and return to the app through its URL scheme; it must work for any server address, so no Associated Domains or Digital Asset Links.

Forces:

- **WebAuthn binds a credential to an RP ID** (a host name) and checks the origin of every ceremony; it needs a secure context (`https`, or `http://localhost`) and cannot use an IP address. A self-hosted server must therefore know the address its users open.
- **Web views cannot use passkeys.** A `WKWebView` gets WebAuthn only for domains in the app's Associated Domains, an Android WebView only with Digital Asset Links; both are verified through files on a public domain (the reason ADR 0036 has no universal links). The system browser has the user's passkeys for any origin.
- **The app's session is the refresh cookie** in its web view (ADR 0021, ADR 0036). Whatever the system browser does, the app must end up with that cookie, without a token in a URL that a custom-scheme handler, a log or the history could leak.
- **More redirect logins follow.** The next planned method is GitHub (a `RedirectAuthenticator`); the app must be able to use it the same way without new hand-off code.

## Decision

**Library: py_webauthn** (`webauthn` on PyPI, maintained by Duo Labs / Cisco, BSD-3-Clause, compatible with the AGPL-3.0 service), version 3.0.1 (2026-09-25, older than the two-week rule), pinned with hashes in `uv.lock` like every dependency. It builds the option sets and verifies challenge, origin, RP ID hash, user presence and verification, the attestation format and the signature. New transitive packages: `cbor2` (MIT), `pyasn1`, `pyasn1-modules` (BSD-2-Clause); `cryptography` and `pyOpenSSL` were already locked. The library sits behind the port `PasskeyCeremonies` (`adapters/webauthn.py`), so the use cases do not import it.

**The login is a plug-in.** `PasskeyAuthenticator` is a `CredentialAuthenticator` (credentials `{ceremony_id, credential}`) and the first `ChallengeAuthenticator`, a credential authenticator that hands out a single-use challenge first (`challenge()`). It is registered in the `AuthenticatorRegistry` only when a relying party is configured; `GET /login-methods` reports `{password, passkey}` from the registry, and the web app shows the button only then. `SessionService.login_passkey` runs it and starts the same session as a password login (`_start_session`: same JWT claims, refresh token rotation, cookie, `session.login` audit with `method: passkey`).

**Relying party from the public URL.** `MOWDECK_IDENTITY_PUBLIC_URL` (Compose `IDENTITY_PUBLIC_URL`, Helm `config.IDENTITY_PUBLIC_URL`, defaulting to `https://<ingress.host>`) gives the only accepted origin; its host is the RP ID unless `MOWDECK_IDENTITY_PASSKEY_RP_ID` names a parent domain. HTTPS is required except for `localhost`, IP addresses are refused. An unusable value turns passkeys off and is logged; it never stops the service, so a typo cannot lock out the password login.

**Ceremonies.** Registration and login require user verification and discoverable credentials (`residentKey: required`), no attestation (`none`: MowDeck does not restrict authenticators), algorithms EdDSA, ES256 and RS256. The user handle is the user id (16 random bytes, no personal data). Challenges are 32 random bytes, stored in `webauthn_challenges` with their purpose (and user for registrations), valid for 5 minutes, consumed (deleted) by the first answer whatever its outcome, purged by the maintenance loop. A login finds the passkey by credential id, compares the returned user handle, verifies the assertion and then the signature counter: when either the stored or the new counter is non-zero, a counter that did not grow refuses the login (WebAuthn section 6.1.1, a cloned authenticator); the passkey stays and the owner decides. Synced passkeys report 0 and pass.

**Credentials** live in `passkeys` (migration 0003): credential id (unique), COSE public key, sign count, transports, AAGUID, name, created and last used, backup eligible ("synced") and backed up, and the user agent of the registration as a device hint. `ON DELETE CASCADE` from `users`.

**Management.** `GET /me/passkeys`, `POST /me/passkeys/options` (the current password again; the per-username failure throttle of the password login applies, failures are counted and audited as `passkey.reauth_failed`), `POST /me/passkeys`, `PATCH` and `DELETE /me/passkeys/{id}`; at most 20 per user. Admins (`can_manage_users`) list and remove the passkeys of the account's sub-users (`/accounts/{id}/users/{id}/passkeys`), the same reach as setting a sub-user's password; persons manage their own. The last passkey can always be removed. Audit: `passkey.added`, `passkey.renamed`, `passkey.removed` (`by_admin`), `passkey.used`, failed logins as `session.login_failed` with `method: passkey` and the reason.

**Rate limits and enumeration.** Answers (`POST /passkeys/session`) have the login limit per client IP (in-process and Traefik `identity-auth`). Challenges check nothing and every sign-in page asks for one (the autofill offer), so they have the looser refresh limit (60 per minute, burst 30; Traefik `identity-passkey-challenge`). No username is ever involved; every failure answers the same 401. Passkey failures write no `login_attempts` row: a passkey has no secret to guess, and failures must not lock the user's password login.

**Native app hand-off, login-method agnostic.** The app does not get a passkey endpoint of its own; it takes over a fresh browser login of any kind:

1. The app keeps a random PKCE verifier in its web view's memory and opens `<origin>/login?app_challenge=<S256(verifier)>&app_state=<random>` with `MowDeckShell.openAuthSession` (iOS `ASWebAuthenticationSession` with an ephemeral session; Android a Custom Tab).
2. In the system browser the user signs in on the normal sign-in page (passkey, or password). The page then calls `POST /app-login/code` with its access token and the challenge: Identity accepts only a user session younger than 5 minutes (a session left in the browser is never handed over silently, `app_login.denied`), ends that browser session in the same transaction, drops the cookie and returns a one-time code (`mda_…`, 256 bits, SHA-256 at rest in `app_login_codes`, valid for 60 s, bound to the challenge, audited `app_login.code_issued`). The page navigates to the fixed `mowdeck://auth/callback?code=…&state=…` and also shows an "Open the app" link (a browser may block the automatic jump without a user gesture). The page never accepts a redirect target.
3. The app receives the callback (iOS as the result of the session, Android as a deep link), checks the state, and redeems code and verifier at `POST /app-login/session`: the code is deleted on the first attempt, a wrong verifier burns it, the user must be active, and the app gets a normal session (refresh cookie in the web view, `session.login` with `method: app_handoff`).

The code in the URL is useless without the verifier, which never leaves the app; another app that registers `mowdeck://` gets nothing it can redeem (RFC 7636). The app's web view stores the session as before (the refresh cookie, ADR 0021); the Keychain is not involved.

**How a redirect authenticator plugs in.** A `RedirectAuthenticator` (GitHub next) only has to start a normal browser session from its callback, as `_start_session` does for passwords and passkeys. On the sign-in page opened for the app, its button keeps the `app_challenge`/`app_state` query through its own `state` (or a return URL to `/login?...`), and after the callback the page calls `POST /app-login/code` exactly as after a passkey login. Nothing in the hand-off knows which method started the session.

Options considered:

- **WebAuthn inside the web view** with Associated Domains / Digital Asset Links: impossible for servers at arbitrary, often internal addresses.
- **A passkey-specific app endpoint** (the browser's assertion returns a code directly): smaller, but every later login method would need its own app path. Rejected for the generic hand-off after the coordinator's design note.
- **Tokens in the callback URL**: leak through custom-scheme hijacking and logs. Rejected.
- **Keeping the browser session** after the hand-off: a Custom Tab shares Chrome's cookies, so a forgotten session would sit in the user's browser. The hand-off ends it.
- **Attestation and an authenticator allow list**: unnecessary for a household installation and would exclude synced passkeys of some providers.
- **Passkeys as a second factor** or passkey-only accounts: not the owner's choice.

## Consequences

- Passkeys work only when `MOWDECK_IDENTITY_PUBLIC_URL` is set and users open MowDeck at exactly that address over HTTPS (or `http://localhost`). Passkeys made for one host do not work at another address of the same server (a LAN name and a Tailscale name are two RP IDs); changing the host later makes existing passkeys unusable (the password still works, and users add new passkeys).
- The native app needs a new build for the hand-off (`MowDeckShell.openAuthSession`); older builds do not show the button. Adding passkeys happens in a browser; the app then uses them through the system browser.
- The login page asks for a challenge on every visit (autofill), which costs one small row each, removed after 5 minutes.
- The CSP needs no change (WebAuthn is not a fetch; the callback is a top-level navigation); the `Permissions-Policy` leaves `publickey-credentials-*` at its default `self`.
- Verified with unit and API tests (a software authenticator through the real library), PostgreSQL migrations up and down, and a Playwright test with Chromium's virtual authenticator (register, passkey login, autofill login, hand-off code redeemed, remove). The native halves (`ASWebAuthenticationSession`, Custom Tab, deep-link return) are verified only by reading and need the owner's devices.
