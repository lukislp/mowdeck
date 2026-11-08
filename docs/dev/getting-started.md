# Getting started

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/) 0.5 or newer
- Docker with Compose v2
- Git

## Linux and macOS

```bash
git clone <repo-url> mowdeck && cd mowdeck
uv sync
uv run poe lint
uv run poe test
uv run poe docs
cp infra/compose/.env.example infra/compose/.env   # set the passwords
docker compose -f infra/compose/docker-compose.yml up -d --wait
```

## Windows

Install Python 3.13, uv, Docker Desktop and Git for Windows. Use PowerShell or Git Bash.

```powershell
git clone <repo-url> mowdeck; cd mowdeck
uv sync
uv run poe lint
uv run poe test
uv run poe docs
Copy-Item infra\compose\.env.example infra\compose\.env
docker compose -f infra/compose/docker-compose.yml up -d --wait
```

## GitHub Codespaces

[Open the repository in a Codespace](https://codespaces.new/lukislp/mowdeck?quickstart=1) for a working environment without installing anything. The configuration is `.devcontainer/` ([ADR 0080](../adr/0080-codespaces-dev-container.md)).

**What you get:** Python 3.13 with uv (`uv sync` done), Node 24 with pnpm (`pnpm install` done), the lint, test and docs commands below, pre-commit hooks, Docker with Compose v2, and the complete stack running inside the Codespace. The machine needs 4 CPUs, 16 GB of memory and 64 GB of storage (`hostRequirements`; pick the 4-core machine type or larger).

**First start:** several minutes. The Codespace installs the toolchains and the dependencies (`on-create.sh`, `update-content.sh`; with a prebuild these are already done), writes the `.env` (`generate-env.sh`), then builds about a dozen images from the checkout and waits until every service is healthy (`post-start.sh`; it first waits for Docker-in-Docker's daemon, and its output is in the creation log and in `.devcontainer/post-start.log`). Later starts reuse the Docker cache and take a minute or two. If the web app does not come up, read that log and run `.devcontainer/post-start.sh` again.

**The web app:** forwarded port 80, labelled "MowDeck (Traefik)", at `https://<codespace-name>-80.app.github.dev` (the Ports tab has the link; the start script prints it too). The port is private: only you, signed in to GitHub, can open it. Register the first account, it becomes the owner, and add a simulated mower. `uv run mkdocs serve -a 0.0.0.0:8000` serves the docs on forwarded port 8000.

**The `.env`:** `.devcontainer/generate-env.sh` writes `infra/compose/.env` from `.env.example` once and never overwrites it. Every secret (database, OpenFGA and NATS passwords, the identity, fleet and vision keys) is random per Codespace and stays in that file, which Git ignores. The origin settings are derived from `CODESPACE_NAME` and `GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN`, because GitHub ends TLS and Traefik receives plain HTTP:

| Key | Value |
| --- | --- |
| `IDENTITY_REFRESH_COOKIE_SECURE` | `true` |
| `IDENTITY_PUBLIC_URL` | the forwarded origin (passkeys) |
| `REALTIME_ALLOWED_ORIGINS` | the forwarded origin and `http://localhost` (WebSocket) |
| `NOTIFICATIONS_APP_URL` | the forwarded origin |
| `WEB_HSTS` | `false` |

`GATEWAY_TRUSTED_PROXIES` stays empty on purpose (the forwarder has no stable address to trust), so login throttling counts all requests as one client; fine for one developer. Change values in `infra/compose/.env` and run `docker compose -f infra/compose/docker-compose.yml up -d`.

**Stop and reset:**

```bash
docker compose -f infra/compose/docker-compose.yml stop        # stop, keep the data
docker compose -f infra/compose/docker-compose.yml down -v     # delete containers and data
rm infra/compose/.env && .devcontainer/generate-env.sh         # new secrets (after down -v)
.devcontainer/post-start.sh                                    # start again
```

**Limits:** no Bluetooth bridge, no Matter server and none of the host-network profiles (a Codespace has no access to your LAN, USB or mDNS); mowers are simulated or linked through the manufacturer's cloud. The Codespace stops after its idle timeout and the stack starts again with it.

## Everyday commands

| Command | Purpose |
| --- | --- |
| `uv sync` | install the workspace into `.venv` and update `uv.lock` |
| `uv run poe lint` | ruff check, ruff format check, mypy strict |
| `uv run poe fmt` | auto fix and format |
| `uv run poe test` | pytest with coverage |
| `uv run poe docs` | `mkdocs build --strict` |
| `uv run poe compose-check` | validate the Compose file |
| `uv run mkdocs serve` | live preview of the documentation |

## Repository conventions

Conventional Commits, one task at a time with tests and README, ADR for every architecture decision. See `CONTRIBUTING.md`. The tests in `tools/repo-checks` guard these conventions.
