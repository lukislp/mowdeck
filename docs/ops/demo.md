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

All visitors share the one demo account: they see each other's commands live. Not each other's sign-ins, though: the security center's session list, its security events and its session count, and the account's audit log (`GET /accounts/{id}/audit`, one click away in the API explorer) only show the visitor's own login session, and the audit log leaves out every address, so nobody sees another visitor's browser, network or times.

Live updates (the WebSocket of `/api/v1/realtime/ws`) are limited per login session instead of per user, since every visitor is the same user: 10 sockets per visitor session, 20 per client address (`DEMO_REALTIME_MAX_CONNECTIONS_PER_ADDRESS`) and 500 for the whole demo (`DEMO_REALTIME_MAX_CONNECTIONS`). One visitor cannot take the live view from the others.

Names visitors type in (sites, groups, area names, zones, schedules) lose control and format characters such as right-to-left overrides and zero-width spaces, so nobody can spoof a label the other visitors read.

## What the demo refuses

Every service enforces this itself (403, problem type `urn:mowdeck:problem:demo-mode`), whatever the web app shows:

- registration, password, passkey, GitHub and Apple sign-in, the app hand-off, every change of the account, its users, roles and security settings (the security center's alert switch, ending one or all other sessions; the overview stays readable), logout everywhere;
- personal access tokens, account API tokens, widget credentials, token exchange;
- linking manufacturer accounts, adding, renaming, reconfiguring or removing mowers, Bluetooth bridge links, every base station change (adding, renaming, removing, its firmware, which base station a mower uses);
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

`reset.sh --dry-run` prints the commands without running them; `--no-build` reuses the images of the last build. The script refuses the project name `mowdeck`, any `COMPOSE_PROFILES` (relay, agora, matter and billing do not belong in a demo) and the placeholder secrets. A git-ignored `demo/demo.override.yml` adds host specific settings ([below](#host-specific-settings-demooverrideyml)).

Behind a proxy, set in `demo/demo.env`:

| Setting | Value |
| --- | --- |
| `GATEWAY_TRUSTED_PROXIES` | the proxy's address (as the gateway sees it), so Traefik keeps the `X-Forwarded-For` the proxy sends |
| `GATEWAY_CLIENT_IP_DEPTH` | `1` for one proxy that sets or appends `X-Forwarded-For` (Caddy, nginx), so the rate limits and the services count each visitor instead of the proxy; see [Client addresses behind a proxy](#client-addresses-behind-a-proxy) |
| `REALTIME_ALLOWED_ORIGINS` | the public address, e.g. `https://demo.example.org`, exactly as the browser sends it in `Origin` (scheme and host, no path) |
| `IDENTITY_REFRESH_COOKIE_SECURE` | `true` (HTTPS) |

`REALTIME_ALLOWED_ORIGINS` is checked on every WebSocket handshake of `/api/v1/realtime/ws`. A wrong or missing origin shows up as **HTTP 403 on the handshake** (the browser console reports the WebSocket as failed, the live status and the mower positions stop updating) while every other page works. Check it with the browser's network tab (filter `ws`) or `docker compose -p mowdeck-demo logs realtime`.

### Client addresses behind a proxy

Traefik's rate limits (`demo-ratelimit` on every request, `identity-ratelimit` on "Try the demo" and the sign-in, `identity-session-ratelimit`, `billing-webhook-ratelimit`) and the client address of Identity and Telemetry depend on two settings:

- `GATEWAY_TRUSTED_PROXIES` (`forwardedHeaders.trustedIPs`): from these peers Traefik keeps the incoming `X-Forwarded-For`; from every other peer it deletes it. This alone does **not** change the rate limits: without a source criterion Traefik counts the TCP peer, which behind a proxy is the proxy, so every visitor shares one bucket (10 "Try the demo" a minute and 1200 requests a minute for the whole internet).
- `GATEWAY_CLIENT_IP_DEPTH` (`sourceCriterion.ipStrategy.depth` of every rate limit, `MOWDECK_CLIENT_IP_DEPTH` of the services): the number of trusted proxies in front of the gateway. Traefik counts that many entries from the right of the `X-Forwarded-For` it received; the services, which see one more entry (Traefik appends its own peer), count one further. With `1` and Caddy sending `X-Forwarded-For: <visitor>`, both take the visitor.

| `GATEWAY_CLIENT_IP_DEPTH` | Rate limits count | Identity and Telemetry take |
| --- | --- | --- |
| empty (default) | the TCP peer (as before) | the leftmost entry (as before) |
| `0` | the TCP peer | the gateway's peer |
| `1` | the last entry the proxy sent | the same entry |

**Spoofing.** A depth only reads entries that a trusted proxy wrote, because Traefik deletes the header of every untrusted peer, and it counts from the right, where a client cannot add anything as long as each proxy **overwrites** the header (Caddy with `header_up X-Forwarded-For {client_ip}`) or **appends** to it (nginx `$proxy_add_x_forwarded_for`). Do not set a depth when a trusted proxy passes the client's header on unchanged, and never list in `GATEWAY_TRUSTED_PROXIES` addresses that ordinary clients connect from. A request that reaches the gateway without the proxy has no `X-Forwarded-For` left, so all such requests share one bucket (stricter, not looser).

**Why not `excludedIPs`.** Traefik's alternative strategy walks `X-Forwarded-For` from the right and takes the first entry outside a list of proxy ranges. It is not safer here: a visitor whose real address falls inside such a range is skipped, so the next entry to the left, which the visitor wrote, becomes the key; the list has to be kept in step with `GATEWAY_TRUSTED_PROXIES`; and Traefik ignores it as soon as a depth is set. The depth is exact for a fixed chain of proxies, which is what a demo host has.

The settings for Cloudflare, Caddy and the gateway on an internal Docker network: Caddy resolves the visitor from `CF-Connecting-IP` (trusting Cloudflare's ranges) and sends exactly one `X-Forwarded-For`, so

```dotenv
GATEWAY_TRUSTED_PROXIES=10.250.80.0/28     # the internal edge network only Caddy and the gateway are on
GATEWAY_CLIENT_IP_DEPTH=1
REALTIME_ALLOWED_ORIGINS=https://mowdeck-demo.example.org
IDENTITY_REFRESH_COOKIE_SECURE=true
```

Check it: more than 20 "Try the demo" sign-ins within a minute from one connection get 429, while a second connection (another network, a phone on mobile data) still signs in. Without the depth both would get 429.

### Host specific settings (demo.override.yml)

`reset.sh` uses `demo/demo.override.yml` as a last Compose file when it exists. The file is git-ignored and belongs to the host: join the gateway to an existing network of the proxy, or limit memory and CPU. Example for a proxy on the external network `mowdeck-demo-edge` that reaches the gateway as `mowdeck-demo-gateway`:

```yaml
# infra/compose/demo/demo.override.yml (git-ignored)
services:
  traefik:
    networks:
      mowdeck: {}
      mowdeck-demo-edge:
        aliases: [mowdeck-demo-gateway]
    mem_limit: 256m
    cpus: 0.5
  postgres:
    mem_limit: 512m
networks:
  mowdeck-demo-edge:
    external: true
```

Create the network once (`docker network create --driver bridge --internal --subnet 10.250.80.0/28 mowdeck-demo-edge`); `reset.sh` never removes an external network. With the gateway on the proxy's network the reconnect step after each reset is no longer needed.

The override has the full power of a Compose file, so `reset.sh` compares the merged configuration with and without it before it deletes anything (with `--dry-run` too) and refuses it when

- the service list differs: it adds a service or enables one of a profile (relay, agora, matter, billing);
- any line of the demo mode (`MOWDECK_DEMO_MODE`), the seed secret (`MOWDECK_DEMO_SEED_TOKEN`, the `X-MowDeck-Demo*` header labels), Traefik's entry point and provider flags or a rate limit (`ratelimit` labels and router middlewares) changes.

Everything else is not checked and is the operator's responsibility: keep the file to networks and resource limits, no ports, volumes, environment or other labels. Those belong in `demo.env` or in the repository. The project name, profile and secret checks run before the override is read, so it cannot get around them. `reset.sh` prints `using .../demo.override.yml` when it takes the file.

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
| `DEMO_REALTIME_MAX_CONNECTIONS` | `500` | Live-update sockets of the whole demo (per Realtime instance) |
| `DEMO_REALTIME_MAX_CONNECTIONS_PER_ADDRESS` | `20` | Live-update sockets per client address; each visitor's login session has at most 10 |

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
