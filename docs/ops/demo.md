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

Requirements: Docker with Compose (2.24.4 or later: the demo uses `!reset` and `!override`), a checkout of the repository (the demo builds its images), a TLS proxy or tunnel in front if it is public, and `python3` when you use a [`demo/demo.override.yml`](#host-specific-settings-demooverrideyml) (`reset.sh` checks it with a standard-library script; Debian: `apt install python3`).

```bash
cd infra/compose
cp demo/demo.env.example demo/demo.env     # PowerShell: Copy-Item demo/demo.env.example demo/demo.env
# fill in every change-me value with a fresh secret:
#   openssl rand -hex 32        (POSTGRES_PASSWORD, SERVICE_DB_PASSWORD, OPENFGA_AUTHN_PRESHARED_KEYS, DEMO_SEED_TOKEN)
#   openssl rand -base64 32     (IDENTITY_KEY_ENCRYPTION_KEY)
demo/reset.sh                              # deletes any previous demo, builds, starts, seeds
```

The demo answers at `http://127.0.0.1:8580` (`GATEWAY_HTTP_PORT`). The seed job's last lines are printed at the end; `docker compose -p mowdeck-demo logs demo-seed` shows them again.

### Host ports

`GATEWAY_HTTP_PORT` is the only port the demo publishes. `docker-compose.demo.yml` drops the gateway's HTTPS port (the TLS proxy in front talks plain HTTP to the gateway; no demo router listens on `websecure`) and the host ports of NATS (client and monitor), Postgres and OpenFGA, which the regular stack binds to `127.0.0.1` for local tools. Only the demo's own containers reach them. `GATEWAY_HTTPS_PORT`, `POSTGRES_PORT`, `NATS_*_PORT` and `OPENFGA_*_PORT` in `demo.env` have no effect; for a look inside use `docker compose -p mowdeck-demo exec postgres psql -U postgres` and the like.

NATS has no authentication, in the demo and in the regular stack: any process that reaches it can publish commands and telemetry past every authorization check. That is why no stack publishes it beyond loopback and the demo not at all; never attach a NATS container to a network that other workloads share. Authenticating the services to NATS (one credential per service) is a planned follow-up with its own ADR.

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

**IPv6.** One IPv6 connection (a home line, a cheap server) usually owns a whole /64, so counting each address would give one client 2^64 buckets. Every rate limit therefore counts an IPv6 client by its /64 (`sourceCriterion.ipStrategy.ipv6Subnet=64` in Compose and in the Helm chart's `-auth-ratelimit`), and so do the in-process limits of Identity (login, re-authentication) and Telemetry (public tracking page) through `mowdeck_service_kit.client_address.limiter_key`. IPv4 counts per address as before. Audit logs keep the whole address; the security center shows it shortened (IPv4 to three octets, IPv6 to its /48). A visitor behind Cloudflare reaches the demo over IPv6 whenever the host name has an AAAA record or Cloudflare's IPv6 compatibility is on; a Cloudflare rate-limiting rule on `/api/v1/identity/demo/session` is a cheap outer layer.

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

The override has the full power of a Compose file, so `reset.sh` checks it against an **allow-list** before it deletes anything (with `--dry-run` too). It renders the stack with `docker compose config --format json` without and with the override and compares the two structurally (`demo/override_guard.py`, standard library only, needs `python3`). The override may only:

| Allowed | Where |
| --- | --- |
| attach an existing service to networks, with aliases or a fixed address | `services.<name>.networks` |
| add networks that exist outside the demo | top-level `networks.<name>` with `external: true` |
| limit resources | `mem_limit`, `mem_reservation`, `memswap_limit`, `cpus`, `cpu_shares`, `cpu_quota`, `cpu_period`, `cpuset`, `pids_limit`, `deploy.resources` of an existing service |
| change logging | `services.<name>.logging` |

Every other difference refuses the reset: a service or a profile (relay, agora, matter, billing, whoami), environment (the demo mode, the seed secret, `MOWDECK_CLIENT_IP_DEPTH`, ...), labels (an extra router, a middleware, a rate limit), Traefik's command (API or dashboard flags, entry points, trusted headers), image, ports, volumes, `network_mode`, other `deploy` keys and any change of the demo's own network. Settings like the demo's history or rate limits belong in `demo.env`; everything else in the repository. The refusal names the paths it refused (for example `services.identity.labels.traefik.http.routers.zz.rule`), never a value: the merged configuration holds the demo's secrets, and it is only compared in memory, never printed or written to a file.

The project name, profile and secret checks run before the override is read, so it cannot get around them. `reset.sh` prints `using .../demo.override.yml` when it takes the file. Without an override `reset.sh` needs no `python3`.

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
