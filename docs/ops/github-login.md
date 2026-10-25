# Sign in with GitHub

Users can link their GitHub account in their settings and then sign in with it. Only linked accounts can sign in (nothing is matched by e-mail address, nobody registers through GitHub), and the password always stays. Design: [ADR 0051](../adr/0051-sign-in-with-github.md).

The GitHub configuration lives only on the server: environment variables and a secret file. The web app never shows it; it only learns that GitHub is on (`GET /api/v1/identity/login-methods` answers `"github": true`).

## 1. Register a GitHub OAuth App

On GitHub: **Settings, Developer settings, OAuth Apps, New OAuth App** (for an organization: the organization's settings, same path). Fields:

| Field | Value |
| --- | --- |
| Application name | `MowDeck` (users see it on GitHub's consent page) |
| Homepage URL | the address users open MowDeck at, e.g. `https://mowdeck.lukas2311-homelab.com` |
| Application description | optional |
| Authorization callback URL | `<IDENTITY_PUBLIC_URL>/api/v1/identity/oauth/github/callback`, e.g. `https://mowdeck.lukas2311-homelab.com/api/v1/identity/oauth/github/callback` |
| Enable Device Flow | off |

After **Register application**: note the **Client ID**, then **Generate a new client secret** and copy it once (GitHub shows it only now). Do not paste the secret into chat, tickets or the `.env` file; it goes into a file (below).

The callback URL must equal `IDENTITY_PUBLIC_URL` (scheme, host, port) plus the path above; Identity logs the exact URL it uses at startup (`GitHub login is on`, `callback_url=…`). GitHub never connects to your server: the user's browser carries the redirect, so a LAN-only installation works. Identity itself must reach `github.com` and `api.github.com` over HTTPS (outbound).

## 2. Configure Identity

Requirements: `IDENTITY_PUBLIC_URL` is set to the address users open (HTTPS; `http://localhost` only for development), see [Passkeys](passkeys.md).

### Docker Compose

1. Put the client secret into a file the identity container (uid 10001) can read, without echoing it to the shell history:

   ```sh
   cd infra/compose   # e.g. /opt/mowdeck/infra/compose
   read -rs GH_SECRET && printf '%s' "$GH_SECRET" | sudo install -o 10001 -g 10001 -m 600 /dev/stdin secrets/github/client_secret && unset GH_SECRET
   ```

   (`read -rs` waits for the pasted secret and Enter.) `infra/compose/secrets/github/` is git-ignored and mounted read-only at `/run/secrets/github` in the identity container.

2. In `infra/compose/.env`:

   ```sh
   IDENTITY_GITHUB_CLIENT_ID=<the Client ID>
   IDENTITY_GITHUB_CLIENT_SECRET_FILE=/run/secrets/github/client_secret
   ```

3. Restart Identity: `docker compose up -d identity` (with a release file: the same `-f docker-compose.yml -f docker-compose.release.yml` as for updates). Then check the log: `docker compose logs identity | grep -i github` shows `GitHub login is on` with the callback URL, or `GitHub login is off` with the reason.

### Helm

```sh
kubectl -n mowdeck create secret generic mowdeck-github --from-file=client-secret=./client-secret
```

```yaml
services:
  identity:
    github:
      enabled: true
      clientId: Iv23li...          # the Client ID
      existingSecret: mowdeck-github
      secretKey: client-secret      # the key in that Secret (default)
config:
  IDENTITY_PUBLIC_URL: ""            # empty: https://<ingress.host>
```

The Secret is mounted as a file at `/run/secrets/github/`; the chart refuses to render with `enabled: true` but no `clientId` or `existingSecret`.

### Settings reference

| Variable | Compose (`.env`) | Default | Meaning |
| --- | --- | --- | --- |
| `MOWDECK_IDENTITY_GITHUB_CLIENT_ID` | `IDENTITY_GITHUB_CLIENT_ID` | empty (off) | Client ID of the OAuth App |
| `MOWDECK_IDENTITY_GITHUB_CLIENT_SECRET_FILE` | `IDENTITY_GITHUB_CLIENT_SECRET_FILE` | empty | File with the client secret |
| `MOWDECK_IDENTITY_GITHUB_AUTHORIZE_URL` | not set | `https://github.com/login/oauth/authorize` | Only for GitHub Enterprise Server (`https://<host>/login/oauth/authorize`) |
| `MOWDECK_IDENTITY_GITHUB_TOKEN_URL` | not set | `https://github.com/login/oauth/access_token` | Only for GitHub Enterprise Server |
| `MOWDECK_IDENTITY_GITHUB_API_URL` | not set | `https://api.github.com` | GitHub Enterprise Server: `https://<host>/api/v3` |

GitHub is on only when the client id, the secret and a usable `IDENTITY_PUBLIC_URL` are all set. A missing piece turns it off with an error in the log; the service and the password login keep running.

## What users do

1. Sign in with the password, **Einstellungen**, **Konto**, panel **GitHub**, **Mit GitHub verknüpfen**, enter the password, approve on GitHub. The panel then shows "Verknüpft mit GitHub als <login>".
2. From then on **Mit GitHub anmelden** on the sign-in page (in the apps it opens the system browser like the passkey sign-in).
3. **Verknüpfung lösen** removes the link at any time; the password keeps working.

See the user guide [Signing in with GitHub](../guides/github-login.md).

## Security notes

- Scope `read:user` only; MowDeck reads GitHub's numeric user id (the link) and the login name (display). E-mail addresses are never requested.
- Authorization code flow with PKCE S256; the single-use `state` is bound to the browser by an `HttpOnly` cookie holding the PKCE verifier (only its hash is stored); round trips expire after 10 minutes.
- The GitHub access token is used for one request (`GET /user`) and dropped; MowDeck never stores or logs it. It is not revoked: revoking an OAuth App's last token deletes the user's authorization, and GitHub would then show its consent page and e-mail "a third-party OAuth application was authorized" at every sign-in ([ADR 0051, amendment 2026-10-25](../adr/0051-sign-in-with-github.md#amendment-2026-10-25-keep-the-github-authorization-no-token-revocation)). GitHub revokes OAuth tokens unused for a year.
- Unlinking in MowDeck does not remove the authorization on GitHub's side (MowDeck holds no token to do it with); the settings panel points the user to <https://github.com/settings/applications>.
- A GitHub account belongs to at most one MowDeck user and a user has at most one GitHub account. Linking needs the current password (the login limits apply).
- Rate limits: the callback and the link start are under the login limit (Traefik `identity-auth`, Helm `-auth` Ingress, and in-process); `POST /api/v1/identity/oauth/github/start` has the looser limit of the passkey challenge.
- Audit log: `external_identity.linked`, `external_identity.unlinked`, `external_identity.link_failed`, `external_identity.reauth_failed`, `session.login` / `session.login_failed` with `method: github`.
- Rotating the client secret: generate a new one on GitHub, replace the file, restart Identity, then delete the old secret on GitHub. Turning GitHub off (empty client id) keeps the links; users can still remove them.

## Database

Migration `0004` of Identity adds `external_identities` and `oauth_states`; it runs with the usual `identity-migrate` job. Downgrading to `0003` deletes all GitHub links (passwords and passkeys stay).

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| No "Mit GitHub anmelden" button, no GitHub panel | GitHub is off: see `GitHub login is off` in the Identity log (client id, secret file or `IDENTITY_PUBLIC_URL` missing or unusable) |
| GitHub shows "The redirect_uri is not associated with this application" | The callback URL of the OAuth App differs from the one Identity logs (host, port, scheme) |
| "GitHub ist gerade nicht erreichbar" | Identity cannot reach github.com / api.github.com, or the client secret is wrong (`incorrect_client_credentials` in the log) |
| "Anmeldung mit GitHub nicht möglich" | The GitHub account is not linked, or the user is disabled (`session.login_failed` in the audit log has the reason) |
| "Die Anmeldung mit GitHub ist abgelaufen" | More than 10 minutes on GitHub, cookies blocked, or another tab started a GitHub sign-in meanwhile |
| "Dieses GitHub-Konto ist schon mit einem anderen MowDeck-Benutzer verknüpft" | That user must unlink it first |
