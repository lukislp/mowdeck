# Public demo

A demo installation lets anybody try MowDeck without an account: a click on **Try the demo** signs the visitor in to a prepared account with three simulated mowers and a week of history. No real mower, manufacturer account, push service or AI provider is reachable, nothing can be registered, and the whole demo is deleted and rebuilt every few hours. Design and the complete list of what the demo refuses: [ADR 0068](../adr/0068-public-demo-mode.md).

Run a demo only as its **own stack**: its own host or VM if possible, its own Compose project, database, volumes, network and secrets. Never on top of, next to the data of, or with the secrets of an installation with real mowers.

## What visitors get

- Three simulated mowers in three gardens: **Clover** (family garden with a pond and an apple tree, Bluetooth bridge for manual driving), **Juniper** (an orchard meadow with trees and beehives, Bluetooth bridge) and **Pebble** (two small lawns, a firmware update on offer).
- Seven days of history per mower (`DEMO_HISTORY_DAYS`): tracks, coverage and missed spots, Wi-Fi, mobile and RTK heatmaps, the health history and battery statistics.
- A weekly schedule per mower with angle rotation; static, dry weather.
- Everything that works on simulated mowers: start, pause, resume, dock, emergency stop, manual driving with the camera test pattern, spot mowing, path previews, mowing defaults, area names, device settings, firmware update, sites and groups, schedules, the in-app notification inbox, theft tracking and the AI assistant with a built-in demo model.
- A band above every page ("Demo · simulated mowers, shared with every visitor, resets every 6 h"), "Demo" in front of every page title and a demo badge on the sign-in page. None of them can be dismissed. Every response of the API carries `X-MowDeck-Demo: 1`.

- The interactive API explorer at `/api-explorer` ([ADR 0072](../adr/0072-api-explorer-in-the-web-app.md)), linked from the band ("Try the API live") and the sign-in page: every service's OpenAPI document in Scalar (self-hosted, nothing from a CDN), "Test Request" with the visitor's demo session. `docker-compose.demo.yml` turns it on (`MOWDECK_WEB_API_EXPLORER=true`); the API docs point visitors to `/api-explorer` of the demo, so once the demo has its public address, link that address in the README and the portfolio.

All visitors share the one demo account: they see each other's commands live. Not each other's sign-ins, though: the security center's session list, its security events and its session count only show the visitor's own login session, so nobody sees another visitor's browser, network or times.

## What the demo refuses

Every service enforces this itself (403, problem type `urn:mowdeck:problem:demo-mode`), whatever the web app shows:

- registration, password, passkey, GitHub and Apple sign-in, the app hand-off, every change of the account, its users, roles and security settings (the security center's alert switch, ending one or all other sessions; the overview stays readable), logout everywhere;
- personal access tokens, account API tokens, widget credentials, token exchange;
- linking manufacturer accounts, adding, renaming, reconfiguring or removing mowers, Bluetooth bridge links, adding base stations;
- Web Push, APNs, Live Activities, ntfy and test notifications (the demo has no delivery channel at all);
- an own AI provider and picture uploads;
- plan and billing changes, the scheduler's development endpoints.

## Run it

Requirements: Docker with Compose, a checkout of the repository (the demo builds its images), and a TLS proxy or tunnel in front if it is public.

```bash
cd infra/compose
cp demo/demo.env.example demo/demo.env     # PowerShell: Copy-Item demo/demo.env.example demo/demo.env
# fill in every change-me value with a fresh secret:
#   openssl rand -hex 32        (POSTGRES_PASSWORD, SERVICE_DB_PASSWORD, OPENFGA_AUTHN_PRESHARED_KEYS, DEMO_SEED_TOKEN)
#   openssl rand -base64 32     (IDENTITY_KEY_ENCRYPTION_KEY)
demo/reset.sh                              # deletes any previous demo, builds, starts, seeds
```

The demo answers at `http://127.0.0.1:8580` (`GATEWAY_HTTP_PORT`). The seed job's last lines are printed at the end; `docker compose -p mowdeck-demo logs demo-seed` shows them again.

`reset.sh --dry-run` prints the commands without running them; `--no-build` reuses the images of the last build. The script refuses the project name `mowdeck`, any `COMPOSE_PROFILES` (relay, agora, matter and billing do not belong in a demo) and the placeholder secrets.

Behind a proxy, set in `demo/demo.env`:

| Setting | Value |
| --- | --- |
| `GATEWAY_TRUSTED_PROXIES` | the proxy's address, so the rate limits see the visitors' addresses |
| `REALTIME_ALLOWED_ORIGINS` | the public address, e.g. `https://demo.example.org` |
| `IDENTITY_REFRESH_COOKIE_SECURE` | `true` (HTTPS) |

## Settings

In `infra/compose/demo/demo.env` (read after `../.env.example`):

| Setting | Default | Meaning |
| --- | --- | --- |
| `DEMO_SEED_TOKEN` | (required) | Secret of the seed job; Traefik strips its header from every outside request |
| `DEMO_RESET_HOURS` | `6` | Shown in the banner; run the timer below at the same interval |
| `DEMO_HISTORY_DAYS` | `7` | Days of simulated history per mower (1 to 31) |
| `DEMO_SIM_TIME_SCALE` | `3` | Simulated seconds per real second of the live mowers |
| `DEMO_RATE_PER_MINUTE`, `DEMO_RATE_BURST` | `1200`, `600` | Requests per client address at the gateway; the web app's files count too, so the burst must hold a first visit with an empty cache (a few hundred requests) |
| `DEMO_USERNAME` | `demo` | The shared demo user |

`MOWDECK_DEMO_MODE` (every service) and `MOWDECK_WEB_DEMO_MODE` (web app) are set by `docker-compose.demo.yml`; they are not meant for an installation with real mowers.

## Periodic reset

Every reset deletes the demo's volumes, so nothing a visitor did survives it, and every visitor's session ends. Run `reset.sh` with a systemd timer (or cron) at the interval of `DEMO_RESET_HOURS`:

```ini
# /etc/systemd/system/mowdeck-demo-reset.service
[Unit]
Description=Reset the MowDeck public demo

[Service]
Type=oneshot
User=mowdeck
WorkingDirectory=/opt/mowdeck/infra/compose
ExecStart=/opt/mowdeck/infra/compose/demo/reset.sh --no-build
```

```ini
# /etc/systemd/system/mowdeck-demo-reset.timer
[Unit]
Description=Reset the MowDeck public demo every 6 hours

[Timer]
OnCalendar=00/6:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now mowdeck-demo-reset.timer
```

Use `--no-build` in the timer and run `reset.sh` without it after updating the checkout. A reset takes about two minutes; visitors see the sign-in page meanwhile.

## Check it

```bash
curl -sI http://127.0.0.1:8580/ | grep -i x-mowdeck-demo            # X-MowDeck-Demo: 1
curl -s -X POST http://127.0.0.1:8580/api/v1/identity/register \
  -H 'Content-Type: application/json' -d '{}' | grep demo-mode        # refused
```

The Playwright specs of the demo run against a test stack with the demo override: `MOWDECK_E2E_DEMO=1 pnpm e2e:stack up`, then `pnpm e2e:stack demo` (CI job `e2e-demo`).
