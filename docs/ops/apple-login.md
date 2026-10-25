# Sign in with Apple

Users can link their Apple ID in their settings and then sign in with it. Only linked accounts can sign in (nothing is matched by e-mail address, nobody registers through Apple), and the password always stays. Design: [ADR 0052](../adr/0052-sign-in-with-apple.md); it works like [Sign in with GitHub](github-login.md).

The Apple configuration lives only on the server: environment variables and a key file. The web app never shows it; it only learns that Apple is on (`GET /api/v1/identity/login-methods` answers `"apple": true`).

**You need your own Apple Developer account.** Registering a Services ID and a key requires membership of the Apple Developer Program (paid, yearly). Every self-hoster registers their own Services ID with the return URL of their own installation; an installation without one simply does not offer Sign in with Apple. Apple accepts only HTTPS return URLs on a real domain (no `localhost`, no IP address).

## 1. Register at Apple

At <https://developer.apple.com/account> choose **Certificates, Identifiers & Profiles**. The example values are the MowDeck installation at `mowdeck.lukas2311-homelab.com`; use your own address and identifiers.

1. **Enable Sign in with Apple on the App ID.** **Identifiers**, filter **App IDs**, open `app.mowdeck.mobile` (the iOS app's bundle identifier; any App ID of your team works as the primary App ID), tick the capability **Sign in with Apple**, **Save**. (If the portal offers "Enable as a primary App ID", keep that.)
2. **Create the Services ID.** **Identifiers**, **+**, **Services IDs**, **Continue**. Description `MowDeck` (users may see this name on Apple's page), Identifier `app.mowdeck.signin` (a new reverse-DNS identifier, different from every App ID), **Continue**, **Register**.
3. **Configure the Services ID.** Open it, tick **Sign in with Apple**, **Configure**:

   | Field | Value |
   | --- | --- |
   | Primary App ID | `app.mowdeck.mobile` |
   | Domains and Subdomains | `mowdeck.lukas2311-homelab.com` (host only: no scheme, no path, no port) |
   | Return URLs | `<IDENTITY_PUBLIC_URL>/api/v1/identity/oauth/apple/callback`, e.g. `https://mowdeck.lukas2311-homelab.com/api/v1/identity/oauth/apple/callback` |

   **Next**, **Done**, **Continue**, **Save**. The return URL must equal `IDENTITY_PUBLIC_URL` (scheme, host, port) plus that path, character for character; Identity logs the exact URL at startup (`Apple login is on`, `callback_url=…`). If the portal asks you to verify the domain, follow its instructions (the service has no special route for it).
4. **Create the key.** **Keys**, **+**, name `MowDeck Sign in with Apple`, tick **Sign in with Apple**, **Configure**, Primary App ID `app.mowdeck.mobile`, **Save**, **Continue**, **Register**, then **Download** the `AuthKey_<KeyID>.p8` file. Apple lets you download it **once**. Note the **Key ID** (10 characters, also in the file name).
5. **Note the Team ID** (10 characters): **Membership details** in the developer account, or the top right of the portal.

You now have: the Services ID (`app.mowdeck.signin`), the Team ID, the Key ID and the `.p8` file. Treat the `.p8` as a password: do not paste it into chat, tickets or the `.env` file; it goes into a file (below). Anybody with the key can obtain sign-ins for your Services ID until you revoke the key in the portal.

## 2. Configure Identity

Requirement: `IDENTITY_PUBLIC_URL` is set to the address users open (HTTPS), see [Passkeys](passkeys.md).

### Docker Compose (the MS-01 server)

1. Copy the key into the secrets directory, readable by the identity container (uid 10001), then remove your copy:

   ```sh
   cd infra/compose   # e.g. /opt/mowdeck/infra/compose
   sudo install -o 10001 -g 10001 -m 600 ~/AuthKey_ABC1234567.p8 secrets/apple/sign_in_key.p8
   shred -u ~/AuthKey_ABC1234567.p8   # or rm; keep one copy in your password manager
   ```

   `infra/compose/secrets/apple/` is git-ignored and mounted read-only at `/run/secrets/apple` in the identity container.

2. In `infra/compose/.env`:

   ```sh
   IDENTITY_APPLE_SERVICES_ID=app.mowdeck.signin
   IDENTITY_APPLE_TEAM_ID=<Team ID>
   IDENTITY_APPLE_KEY_ID=<Key ID>
   IDENTITY_APPLE_PRIVATE_KEY_FILE=/run/secrets/apple/sign_in_key.p8
   ```

3. Restart Identity: `docker compose up -d identity` (with a release file: the same `-f docker-compose.yml -f docker-compose.release.yml` as for updates). Then check the log: `docker compose logs identity | grep -i apple` shows `Apple login is on` with the return URL, or `Apple login is off` with the reason.

### Helm

```sh
kubectl -n mowdeck create secret generic mowdeck-apple --from-file=sign-in-key.p8=./AuthKey_ABC1234567.p8
```

```yaml
services:
  identity:
    apple:
      enabled: true
      servicesId: app.mowdeck.signin
      teamId: ABCDE12345
      keyId: ABC1234567
      existingSecret: mowdeck-apple
      secretKey: sign-in-key.p8     # the key in that Secret (default)
config:
  IDENTITY_PUBLIC_URL: ""            # empty: https://<ingress.host>
```

The Secret is mounted as a file at `/run/secrets/apple/`; the chart refuses to render with `enabled: true` but a missing `servicesId`, `teamId`, `keyId` or `existingSecret`.

### Settings reference

| Variable | Compose (`.env`) | Default | Meaning |
| --- | --- | --- | --- |
| `MOWDECK_IDENTITY_APPLE_SERVICES_ID` | `IDENTITY_APPLE_SERVICES_ID` | empty (off) | The Services ID (the OAuth client id) |
| `MOWDECK_IDENTITY_APPLE_TEAM_ID` | `IDENTITY_APPLE_TEAM_ID` | empty | Apple Developer Team ID, 10 characters |
| `MOWDECK_IDENTITY_APPLE_KEY_ID` | `IDENTITY_APPLE_KEY_ID` | empty | Key ID of the Sign in with Apple key, 10 characters |
| `MOWDECK_IDENTITY_APPLE_PRIVATE_KEY_FILE` | `IDENTITY_APPLE_PRIVATE_KEY_FILE` | empty | Path of the `.p8` file (unencrypted PKCS#8 PEM, EC P-256) |
| `MOWDECK_IDENTITY_APPLE_PKCE` | `IDENTITY_APPLE_PKCE` | `true` | Send the PKCE challenge and verifier; set `false` only if Apple refuses the requests because of them |
| `MOWDECK_IDENTITY_APPLE_AUTHORIZE_URL`, `_TOKEN_URL`, `_KEYS_URL`, `_ISSUER` | not set | appleid.apple.com | Apple's endpoints; other values only for tests |

Apple is on only when the Services ID, Team ID, Key ID, a readable key file and a usable `IDENTITY_PUBLIC_URL` are all set. A missing or wrong piece (also a key file that does not exist or is not a P-256 key) turns it off with an error in the log; the service and the password login keep running.

## What users do

1. Sign in with the password, **Einstellungen**, **Konto**, panel **Apple**, **Mit Apple verknüpfen**, enter the password, sign in with the Apple ID at Apple. The panel then shows "Verknüpft mit Apple".
2. From then on **Mit Apple anmelden** on the sign-in page (in the apps it opens the system browser like the passkey sign-in).
3. **Verknüpfung lösen** removes the link at any time; the password keeps working.

See the user guide [Signing in with Apple](../guides/apple-login.md).

## Security notes

- No scope is requested: MowDeck gets neither the name nor the e-mail address. It reads only the ID token's `sub` (Apple's stable id of the user for your team) and shows the neutral label "Apple ID". E-mail claims in the token, including a private relay address, are ignored.
- Authorization code flow; the single-use `state` is bound to the browser by an `HttpOnly` cookie holding the PKCE verifier (only its hash is stored); the ID token must carry the round trip's nonce; round trips expire after 10 minutes. Because no scope is requested, Apple returns to the callback with a redirect (`response_mode=query`), which keeps that cookie.
- The ID token is validated: signature against Apple's published keys, issuer, audience (your Services ID), expiry and issue time, nonce.
- The client secret Apple wants is a short-lived (one hour) JWT that Identity signs itself with your key; there is no secret that expires every six months. Rotating the key: create a new key at Apple, replace the file, restart Identity, then revoke the old key in the portal.
- Apple's tokens are not stored and not revoked (see ADR 0052). Unlinking in MowDeck removes only MowDeck's link; to remove the authorization at Apple as well, the user opens their Apple ID settings, **Sign-In & Security**, **Sign in with Apple**, and stops using MowDeck there. Sign-in then asks for Apple's consent again.
- An Apple account belongs to at most one MowDeck user and a user has at most one Apple ID. Linking needs the current password (the login limits apply).
- Rate limits: the callback and the link start are under the login limit (Traefik `identity-auth`, Helm `-auth` Ingress, and in-process); `POST /api/v1/identity/oauth/apple/start` has the looser limit of the passkey challenge.
- Audit log: `external_identity.linked`, `external_identity.unlinked`, `external_identity.link_failed`, `external_identity.reauth_failed`, `session.login` / `session.login_failed` with `method: apple`.
- Turning Apple off (empty Services ID) keeps the links; users can still remove them.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| No "Mit Apple anmelden" button, no Apple panel | Apple is off: see `Apple login is off` in the Identity log (an ID of the wrong shape, key file missing or not a P-256 key, `IDENTITY_PUBLIC_URL` missing or not HTTPS) |
| Apple shows `invalid_request` or "Invalid redirect_uri" | The Return URL of the Services ID differs from the one Identity logs, or the domain is not listed under Domains and Subdomains |
| "Apple ist gerade nicht erreichbar" | Identity cannot reach appleid.apple.com, or `invalid_client` in the log: wrong Services ID, Team ID, Key ID or key, or the key was revoked |
| "Anmeldung mit Apple nicht möglich" | The Apple account is not linked, the user is disabled, or the ID token was refused (`session.login_failed` in the audit log has the reason) |
| "Die Anmeldung mit Apple ist abgelaufen" | More than 10 minutes at Apple, cookies blocked, or another tab started a sign-in meanwhile |
| "Diese Apple-ID ist schon mit einem anderen MowDeck-Benutzer verknüpft" | That user must unlink it first |
| Apple refuses with an error that mentions `code_challenge` or `code_verifier` | Set `IDENTITY_APPLE_PKCE=false` (Helm: `services.identity.extraEnv.MOWDECK_IDENTITY_APPLE_PKCE: "false"`) and report it |
