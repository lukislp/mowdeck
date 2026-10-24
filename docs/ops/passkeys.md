# Passkeys

Users can sign in with a passkey instead of their password (Face ID, Touch ID, Windows Hello, a phone or a security key); the password always stays as the fallback. Design: [ADR 0050](../adr/0050-passkeys-and-app-login-hand-off.md).

## What the server needs

Passkeys are bound to the address users open MowDeck at. Tell Identity that address:

| Setting | Compose (`infra/compose/.env`) | Helm (`config`) | Meaning |
| --- | --- | --- | --- |
| `MOWDECK_IDENTITY_PUBLIC_URL` | `IDENTITY_PUBLIC_URL` | `IDENTITY_PUBLIC_URL` (empty: `https://<ingress.host>`) | The address in the browser's address bar, e.g. `https://mowdeck.example.org`. Empty turns passkeys off. |
| `MOWDECK_IDENTITY_PASSKEY_RP_ID` | `IDENTITY_PASSKEY_RP_ID` | `IDENTITY_PASSKEY_RP_ID` | Optional. A parent domain of that host (e.g. `example.org`) when passkeys should also work on other subdomains. |
| `MOWDECK_IDENTITY_PASSKEY_RP_NAME` | not set | not set | Name shown next to the passkey (default `MowDeck`). |

Rules the browser enforces:

- **HTTPS.** Passkeys work only in a secure context: `https://`, or `http://localhost` for development. Over plain HTTP in the LAN the button does not appear. A private CA is fine as long as the devices trust it (the same requirement as the refresh cookie, see [Mobile apps](mobile-apps.md)).
- **A host name, not an IP address.** `https://192.168.1.20` cannot carry passkeys; give the server a name (local DNS, Tailscale MagicDNS, a public domain).
- **One address.** A passkey made at `https://mowdeck.home.lan` does not work at `https://mowdeck.tailnet.ts.net`. Pick the address everybody uses. Changing the host later makes existing passkeys useless (users sign in with their password and add new ones).
- **Exact origin.** A port belongs to the origin: `https://mowdeck.example.org:8443` is a different origin from `https://mowdeck.example.org`. Set the URL exactly as users type it.

When the value is not usable (plain HTTP, an IP address, a wrong RP ID), Identity logs `passkeys are off: MOWDECK_IDENTITY_PUBLIC_URL is not usable` with the reason and keeps running with passwords only. `GET /api/v1/identity/login-methods` shows `{"password": true, "passkey": true}` once it works.

Restart Identity after changing the settings (Compose: `docker compose up -d identity`).

## Gateway and rate limits

Compose's Traefik and the Helm `-auth` Ingress put `POST /api/v1/identity/passkeys/session` and `POST /api/v1/identity/app-login/session` under the login rate limit (10 per minute, burst 20 per client address). `POST /api/v1/identity/passkeys/challenge` is asked for by every sign-in page (the autofill offer) and has the looser limit of refreshes (60 per minute, burst 30). Identity applies the same limits in-process.

## The apps

The iOS and Android apps sign in with a passkey through the system browser (Safari, Chrome) and return to the app with a one-time code; this needs an app build that has `MowDeckShell.openAuthSession` (older builds show only the password form). Passkeys are added in a browser (Einstellungen, Konto, Passkeys); the app then uses them. Nothing else is configured for the apps: no Associated Domains, no Digital Asset Links.

## Database

Migration `0003` of Identity adds `passkeys`, `webauthn_challenges` and `app_login_codes`; it runs with the usual `identity-migrate` job. Downgrading to `0002` deletes all passkeys (passwords stay).

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| No "Mit Passkey anmelden" button | `IDENTITY_PUBLIC_URL` empty or unusable (see the log), the page is not served over HTTPS, or the browser has no WebAuthn |
| The browser says the passkey is not for this site, or creating fails | The address in the address bar differs from `IDENTITY_PUBLIC_URL` (host, port or scheme) |
| "Dieser Passkey wurde nicht akzeptiert" | The passkey was removed, belongs to another installation, or its counter went backwards (the audit log has the reason under `session.login_failed`) |
| In the app the browser opens but the app stays on the sign-in page | Android blocked the automatic jump: tap "App öffnen"; or more than a minute passed (the code expired), sign in again |
