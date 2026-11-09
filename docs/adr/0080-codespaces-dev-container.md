# ADR 0080: GitHub Codespaces dev container with the full Compose stack

## Status

Accepted (2026-11-07).

## Context

A contributor needs Python 3.13, uv, Node 24, pnpm, Docker with Compose v2 and the docs toolchain, plus a stack that is running before they can try a change. Getting that on a fresh machine takes an afternoon. GitHub Codespaces can provide it in a browser tab, if the Compose stack runs inside the Codespace and its web app is reached through GitHub's port forwarding.

The forwarder changes what the stack sees. A Codespace's port 80 is published as `https://$CODESPACE_NAME-80.$GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN`. TLS ends at the forwarder; Traefik receives plain HTTP, with a forwarder address as the TCP peer. Several settings depend on the public origin: the refresh cookie's `Secure` flag, the realtime WebSocket's origin check, the passkey host name, links in notifications, and HSTS.

## Decision

One configuration, `.devcontainer/`, no variants:

- The base image `mcr.microsoft.com/devcontainers/python:1-3.13-bookworm`, pinned by digest in `.devcontainer/Dockerfile`, which also removes the image's Yarn apt repository (its signing key is no longer accepted, which made every `apt-get update` and so every feature install fail; nothing here needs Yarn), with the Dev Container features for Node (pinned to `.node-version`), Docker-in-Docker and the GitHub CLI. uv is copied into the image from `UV_IMAGE` of `infra/compose/.env.example` (pinned by digest, kept equal by `tools/repo-checks/tests/test_devcontainer.py`), pnpm through Corepack at the version of `package.json` (`packageManager`), so the Codespace and the stack's images agree. `COREPACK_ENABLE_DOWNLOAD_PROMPT=0` (`containerEnv`) lets Corepack fetch that pnpm without asking: a prebuild has no terminal to answer its download prompt and waited on it.
- `hostRequirements` of 4 CPUs, 16 GB of memory and 64 GB of storage, because the stack builds about a dozen images from the checkout.
- The lifecycle commands are split so that a Codespaces prebuild can do the heavy part: `onCreateCommand` (`on-create.sh`) installs uv and pnpm, `updateContentCommand` (`update-content.sh`) runs `uv sync`, `pnpm install` and installs the pre-commit hook; both run when the container is built, also inside a prebuild, and `updateContentCommand` runs again when a Codespace opens from a prebuild older than the branch. `postCreateCommand` (`generate-env.sh`) writes `infra/compose/.env` once per Codespace, never in a prebuild, because the secrets and the public origin belong to one Codespace. `postStartCommand` (`post-start.sh`) runs `docker compose up -d --build --wait` on every start, which is idempotent and quick when the Docker cache is warm; the image builds stay out of the prebuild because the Docker daemon is not reliably available there.
- `generate-env.sh` writes the `.env` from `.env.example` and never overwrites an existing one. Every secret (the PostgreSQL passwords, the OpenFGA key, the NATS passwords through `nats-passwords.sh`, the identity, fleet and vision keys) is random and per Codespace. Nothing secret is committed; `.env` is git-ignored.
- The values derived from the public origin `https://$CODESPACE_NAME-80.$GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN`:

  | Key | Value | Why |
  | --- | --- | --- |
  | `IDENTITY_REFRESH_COOKIE_SECURE` | `true` | The browser talks HTTPS to the forwarder, so the refresh cookie gets the `Secure` flag as in any HTTPS installation. |
  | `REALTIME_ALLOWED_ORIGINS` | the origin and `http://localhost` | The browser sends the forwarded origin on the WebSocket; the default `http://localhost` would refuse it. |
  | `IDENTITY_PUBLIC_URL` | the origin | Passkeys are bound to this host name and need HTTPS. |
  | `NOTIFICATIONS_APP_URL` | the origin | Links in notifications. |
  | `WEB_HSTS` | `false` | The host name disappears with the Codespace, so the browser should not pin it for a year. |

- `GATEWAY_TRUSTED_PROXIES` and `GATEWAY_CLIENT_IP_DEPTH` stay empty. Traefik then replaces `X-Forwarded-*` with its own view of the connection. The forwarder's address is not a documented, stable range, so naming it would be a guess, and trusting a wide range would let a client fake its address. The only consumer of `X-Forwarded-Proto` here is HSTS (off), plus the disabled billing service. The cost: every request appears to come from one address, so Identity's per-IP login throttling is one shared bucket. That is acceptable for a development environment with one user.
- The Codespace's ports are private by default (GitHub authenticates every request to the forwarded URL); the setup does not make them public.

Options considered: Docker Compose through the repository's own services only (no Compose stack), which would leave the web app without its backend; a Compose-based dev container with the stack as sibling containers, which needs the Docker socket of the host and is not available in Codespaces; a separate configuration for a lightweight Codespace without the stack, which doubles the maintenance for a machine size that is not enough for the web build anyway.

## Consequences

- "Open in Codespaces" gives a contributor a working checkout with the stack running and the web app open; the first start takes several minutes (toolchains, then the image builds), later starts are fast.
- It needs a machine type with 4 CPUs and 16 GB; the free tier's smaller machines are refused by `hostRequirements`.
- What a Codespace cannot do: the Bluetooth bridge, the Matter server (host networking and mDNS) and the other host-network profiles are not available; a mower is simulated or linked through its cloud.
- Nothing else depends on the public origin: the web app and the APIs use relative URLs and the Host header they receive, and no service has a setting for the forwarder's address beyond the ones above.
- Verification of the forwarded flow (registration, login, WebSocket) can only be done in a real Codespace; the CI of this repository checks the files, not a Codespace.
- Follow-up: if the forwarder turns out to send a reliable `X-Forwarded-For`, trusting it with `GATEWAY_CLIENT_IP_DEPTH=1` would give per-client throttling.
- Dependabot keeps the dev container current (`.github/dependabot.yml`): the `devcontainers` ecosystem bumps the features in `devcontainer.json` and `devcontainer-lock.json` in one grouped pull request, the `docker` ecosystem on `/.devcontainer` bumps the digest of the base image in the `Dockerfile`. Both arrive as `ci:` commits and release nothing.
