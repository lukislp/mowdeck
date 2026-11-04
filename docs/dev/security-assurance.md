# Security assurance case

This page argues why MowDeck's security requirements are met. It covers the threat model, the trust boundaries, how the secure design principles are applied, how common implementation weaknesses are countered, and which risks are knowingly accepted. It pulls the security-relevant decisions of the ADRs and the operations guides into one argument and links to them for detail. How to report a vulnerability is in [SECURITY.md](https://github.com/lukislp/mowdeck/blob/main/SECURITY.md).

Last full review: **2026-11-04** (maintainer, see [Review record](#review-record)).

## Claim

> A MowDeck installation deployed as documented keeps each account's mowers, maps, schedules, camera images and linked vendor credentials confidential and intact against other accounts, against unauthenticated network clients and against API tokens acting outside the grants their creator gave them. Only someone with the matching right can move a mower, and the emergency stop is never blocked by MowDeck. When a credential, a token or an input is invalid, the request is rejected.

What it does **not** claim: protection against an attacker who already controls the host, a container, the database or the cluster; protection of a user's own unlocked device; the security of the mower vendors' clouds, apps and firmware; availability under a determined denial-of-service attack beyond rate limiting.

## Assets

| Asset | Where it lives | Why it matters |
| --- | --- | --- |
| Account data: mowers, maps, zones, schedules, telemetry and position history, camera frames | PostgreSQL, one database per service ([ADR 0006](../adr/0006-postgresql-for-all-persistence.md)) | The household's private data; positions tell when nobody is home |
| Mower control | Fleet, Device-Gateway, the vendor clouds and the Bluetooth bridge | A moving mower with blades is a physical hazard ([ADR 0012](../adr/0012-mower-control-interface-and-safety-rules.md)) |
| User credentials: password hashes, passkey public keys, refresh tokens, API tokens, device cookies | Identity's database. Passwords are stored as Argon2id hashes and tokens as SHA-256 of random values | Account takeover |
| Linked vendor credentials (Mammotion, Husqvarna, Worx) and AI provider API keys | Fleet and Vision databases, AES-256-GCM at rest ([ADR 0034](../adr/0034-linked-cloud-accounts-and-device-discovery.md)) | Takeover of the user's vendor account, costs on the user's AI account |
| Server secrets: token signing keys, key-encryption keys, NATS passwords, OpenFGA key, VAPID/APNs keys | Identity's database (signing keys, encrypted), environment or secret files | Forging tokens, impersonating services |
| Release artifacts | `ghcr.io/lukislp/mowdeck`, GitHub Releases | Supply-chain compromise of every installation |

## Actors and threat sources

- **Anonymous network client**: reaches the gateway, the login and registration endpoints, the public demo, and a theft tracking share link (256-bit token, only its SHA-256 stored, at most 48 hours, revocable, [ADR 0058](../adr/0058-theft-tracking-mode.md)).
- **Signed-in user**: has a valid session. Must not reach another account's data, and inside an account only what the user's role and grants allow ([ADR 0014](../adr/0014-authorization-model-and-tuple-ownership.md)).
- **API token** (Home Assistant, scripts, personal access tokens): limited to the grants its creator gave it, never more than the creator holds ([ADR 0064](../adr/0064-personal-access-tokens-and-public-api-docs.md)).
- **Demo visitor**: an anonymous user of the public demo with a throwaway sandbox ([ADR 0068](../adr/0068-public-demo-mode.md), [ADR 0076](../adr/0076-demo-sandboxes.md)).
- **Compromised service or container** inside the stack: limited by per-service NATS users and subject permissions, per-service databases, and the fact that every service checks the caller's token itself. It is partly out of scope (see the residual risks).
- **Hostile vendor cloud or network peer**: sends malformed or oversized frames to the adapters.
- **Supply-chain attacker**: a tampered dependency, action or image.

## Trust boundaries

| # | Boundary | Controls at the boundary |
| --- | --- | --- |
| TB1 | Internet or LAN to the gateway | Traefik is the only entry point ([ADR 0008](../adr/0008-traefik-as-gateway.md)). TLS is terminated by the operator's reverse proxy, Tailscale or the Kubernetes ingress ([Compose, HTTPS](../ops/compose.md), [Helm](../ops/helm.md)). Rate limits on the login, registration and token routes. Trusted proxies have to be configured explicitly (`GATEWAY_TRUSTED_PROXIES`). |
| TB2 | Browser to web app | Strict CSP built with script hashes and no inline styles, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy`, COOP (`apps/web/nginx/security-headers.conf`). The access token lives only in memory, the refresh token only in an HttpOnly, `SameSite=Strict` cookie on a narrow path ([ADR 0021](../adr/0021-browser-auth-flow.md)). |
| TB3 | Request to a service | Every service verifies the EdDSA access token locally against Identity's JWKS (`mowdeck-auth`: `alg` allow list, `none` and HMAC refused, `kid` required, issuer, audience and expiry checked) ([ADR 0015](../adr/0015-tokens-sessions-and-key-rotation.md)). Request bodies are typed Pydantic models with bounds, and anything else is answered with a problem response. |
| TB4 | Account A to account B, user to user | OpenFGA checks on every object and every command kind ([ADR 0001](../adr/0001-openfga-for-authorization.md), [ADR 0016](../adr/0016-fleet-commands-and-authorization-per-command-kind.md)). Queries are scoped to the principal's account. Every service has tests that request another account's data and expect a rejection. WebSocket subscriptions are checked per mower and re-checked periodically ([ADR 0018](../adr/0018-realtime-websocket-protocol.md)). |
| TB5 | Service to service | NATS with one user per service and least-privilege subject permissions, generated from one reviewable map ([ADR 0074](../adr/0074-nats-authentication-and-subject-permissions.md)). CI fails on any permission violation. Synchronous calls carry the caller's token or a service token. OpenFGA needs its preshared key. |
| TB6 | MowDeck to mower and vendor cloud | Vendor adapters in their own packages ([ADR 0066](../adr/0066-cloud-provider-plugins-and-more-vendors.md)). Every frame from a vendor is parsed into contracts and fuzzed. The Bluetooth bridge is an ESPHome proxy whose API is encrypted with its own key ([ESP32 bridge](../ops/ble-bridge-esphome.md)). The controller enforces the safety rules for every adapter ([ADR 0012](../adr/0012-mower-control-interface-and-safety-rules.md)). |
| TB7 | Services to user-chosen URLs | Web Push endpoints only on the allowed push service hosts, ntfy only on configured servers, AI provider URLs without private, loopback or single-label hosts on hosted installations (Vision `public` policy). |
| TB8 | Source to release | Required checks and signed commits on `main`, CodeQL, dependency review and Socket on every pull request, SHA-pinned actions behind `harden-runner`, keyless-signed images, chart and release files with SLSA provenance, an SSH-signed tag ([releases](../ops/release.md)). |

## Threat model (STRIDE)

| Threat | Example | Countermeasure | Evidence |
| --- | --- | --- | --- |
| **S**poofing a user | Password guessing, phishing | Argon2id hashes, login throttling per address and per user, passkeys (origin-bound WebAuthn), GitHub and Apple sign-in with PKCE, alerts about new sign-ins ([ADR 0050](../adr/0050-passkeys-and-app-login-hand-off.md), [ADR 0065](../adr/0065-security-center-and-new-login-alerts.md)) | Identity tests, `services/identity/README.md` |
| Spoofing with a stolen session | A copied refresh token | Refresh tokens rotate on every use. A replayed old token ends the whole login session (reuse detection). Critical actions need a recent interactive login (step-up, [ADR 0057](../adr/0057-step-up-reauthentication.md)) | ADR 0015, Identity tests |
| Spoofing a service | A container posing as Fleet on NATS | Per-service NATS users and subject permissions | ADR 0074, `test_nats_auth_server.py` |
| **T**ampering with another account's mower | IDOR on `/mowers/{id}` | OpenFGA object checks; a foreign and a missing mower answer the same | Tenant isolation tests in every service |
| Tampering with commands | Replayed or stale drive command | Command envelopes with id, de-duplication and expiry; drive sessions with an explicit stop ([ADR 0009](../adr/0009-command-and-event-envelopes.md), [ADR 0026](../adr/0026-manual-drive-channel.md)) | `libs/mowdeck-control` tests |
| Tampering with releases | A malicious image | Sigstore signatures and provenance; `verify-release.sh` pins verified digests | [SECURITY.md](https://github.com/lukislp/mowdeck/blob/main/SECURITY.md#verifying-releases) |
| **R**epudiation | Who started the mower? | Commands carry the principal. Security events (logins, step-up, token changes) are logged per user and shown on the security page ([ADR 0065](../adr/0065-security-center-and-new-login-alerts.md)) | [Your security page](../guides/security.md) |
| **I**nformation disclosure across accounts | Positions of another household | Account-scoped queries, OpenFGA checks, `Cache-Control: no-store` on authenticated responses | `mowdeck_service_kit.hardening` |
| Disclosure of credentials | A database dump | Password hashes, SHA-256 token digests, AES-256-GCM for vendor credentials, AI keys and signing keys, with the row id as associated data. The development keys of `.env.example` are refused in production | `fleet.adapters.crypto`, `identity.adapters.crypto` |
| Disclosure through tokens in URLs | WebSocket token in access logs | The token travels in the first WebSocket message, never in the URL ([ADR 0018](../adr/0018-realtime-websocket-protocol.md)) | Realtime tests |
| **D**enial of service | Login floods, oversized frames | Gateway rate limits, Identity throttling, bounded model fields, bounded caches and queues, demo sandbox limits per address | ADR 0076, load tests ([load testing](../ops/load-testing.md)) |
| **E**levation of privilege | An API token used for more than its grants | Tokens are their own OpenFGA subject with explicit grants, capped at the creator's rights; step-up endpoints refuse API tokens | ADR 0064 |
| Elevation in the demo | A visitor reaching out of the demo | `DemoModeMiddleware` in every service refuses the listed mutations, with allow lists in Identity, Entitlements and Billing | ADR 0068 |
| SSRF | A push endpoint pointing into the stack | Allowed push hosts, configured ntfy servers, the public URL policy of Vision | Notifications and Vision tests |

## Secure design principles

How the Saltzer and Schroeder principles show up in the code:

- **Economy of mechanism.** One token format (EdDSA JWTs from Identity, `mowdeck-auth`), one authorization service (OpenFGA), one service kit (`mowdeck-service-kit`) that every service is built on, and one controller that enforces the mower safety rules for every adapter. Crypto comes from `cryptography`, `argon2-cffi` and PyJWT. There are no home-made primitives.
- **Fail-safe defaults.** A route needs a valid token unless it is explicitly public. A missing encryption key disables the feature (503) instead of storing plaintext. Services default to `MOWDECK_ENVIRONMENT=prod`, the strictest setting. Map tiles, camera, AI analysis and other features that talk to third parties are off by default. Unknown `alg` values and tokens without `kid` are refused.
- **Complete mediation.** Every HTTP request and every WebSocket subscription is checked against OpenFGA, per object. Open WebSocket subscriptions are re-checked every 60 seconds and right after a revocation event from Identity ([ADR 0018](../adr/0018-realtime-websocket-protocol.md)).
- **Open design.** The whole system is public, including this threat model. Security does not depend on secrecy of the code.
- **Separation of privilege.** Critical actions need a confirmation with a passkey or the password within the last few minutes (step-up, [ADR 0057](../adr/0057-step-up-reauthentication.md)), and an API token is never enough for them.
- **Least privilege.** Per-service NATS users with the subjects they need and nothing else. Per-service databases. API tokens get only the grants chosen for them. Containers run without capabilities and read-only where possible (Compose `cap_drop`/`read_only`, Helm `runAsNonRoot`, `readOnlyRootFilesystem`, `drop: ["ALL"]`, seccomp `RuntimeDefault`).
- **Least common mechanism.** Refresh tokens, API tokens, device cookies, relay tickets and tracking links are separate secret spaces with their own prefixes and storage, so one can never be used as another.
- **Psychological acceptability.** Passkeys and provider sign-in besides passwords. Pairing Home Assistant needs a single token. The security page shows signed-in devices and recent events in plain language.

## Common implementation weaknesses (OWASP Top 10 2021)

| Category | Countermeasure |
| --- | --- |
| A01 Broken access control | OpenFGA per object and command kind, account-scoped queries, tenant isolation tests in every service, step-up for critical actions |
| A02 Cryptographic failures | Only published algorithms: Ed25519 (EdDSA) signatures, AES-256-GCM, Argon2id, SHA-256, HMAC comparisons in constant time. `secrets`/`os.urandom` for every random value. No MD5, SHA-1, DES or RC4 in the code. TLS at the edge; see [Network encryption](#network-encryption) for the hops inside the stack |
| A03 Injection | SQLAlchemy with bound parameters, no string-built SQL. Svelte output encoding and a strict CSP. No shell execution with user input. ruff's bandit rules (`S`) and CodeQL on every pull request |
| A04 Insecure design | ADRs for every architecture decision, this page, the mower safety rules, bounded inputs |
| A05 Security misconfiguration | Hardening headers set by the web container and the service kit, hardened containers, the chart and the Compose files validated in CI, development keys refused in production |
| A06 Vulnerable and outdated components | Dependabot (pip, uv, npm, GitHub Actions), dependency review, Socket, OSV-Scanner exceptions documented in `osv-scanner.toml`, OpenSSF Scorecard |
| A07 Identification and authentication failures | Argon2id, throttling, passkeys, rotating refresh tokens with reuse detection, 10 minute access tokens, new-login alerts |
| A08 Software and data integrity failures | Locked dependencies (`uv.lock`, `pnpm-lock.yaml`), SHA-pinned actions, signed commits required on `main`, signed releases with provenance |
| A09 Logging and monitoring failures | Structured logs, security events per user, metrics and traces (OpenTelemetry), health endpoints ([ADR 0077](../adr/0077-liveness-readiness-and-routing.md)) |
| A10 SSRF | Allow lists for push and ntfy, the public URL policy of Vision; map tile and aerial photo URLs are operator configuration, not user input |

Static analysis checks for these classes of bugs continuously: CodeQL (default setup for Python, JavaScript/TypeScript and Actions, a ruleset gate for high and critical alerts), ruff with the bandit rules, mypy strict, ESLint and svelte-check.

## Input validation

Every input from a potentially untrusted source is checked before use. Invalid input is rejected, not repaired:

1. **Shape and type.** Every HTTP body and query parameter binds to a typed Pydantic model or a typed parameter. Fields carry bounds (`ge`, `le`, `max_length`, patterns, literal enums). FastAPI answers anything that does not fit with a problem response before the route runs.
2. **Semantics.** Services check what a model cannot express (time ranges, polygon validity, that a mower belongs to the account) and answer with a typed problem.
3. **Messages.** NATS and WebSocket messages are discriminated unions of `mowdeck-contracts` with JSON Schemas. Unknown types are refused.
4. **Vendor data.** Frames from vendor clouds and the Bluetooth bridge are parsed into contracts by the adapters. The parsers are fuzzed continuously ([fuzzing](fuzzing.md)).
5. **URLs.** User-chosen URLs pass an allow list or the public URL policy (TB7).
6. **Credentials.** Credentials are looked up by hash or compared with `hmac.compare_digest`, and never parsed beyond format checks.

The house rule for new code: a new route or model field gets bounds in the model or an explicit check in the service, and a test for the rejection ([CONTRIBUTING.md](https://github.com/lukislp/mowdeck/blob/main/CONTRIBUTING.md#tests)).

## Cryptography and agility

The algorithms are listed under A02. Algorithm agility is limited but real. Fleet's stored vendor credentials carry a format version and a key id (`version || key id || nonce || ciphertext`), so its key rotates without a rebuild (old keys stay readable from `MOWDECK_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS`), and the next read re-seals a credential with the new key. Identity's signing keys and Vision's API keys are bound to their row by the associated data but carry no format version yet, so a new cipher for them needs a migration. Signing keys rotate on a schedule with an announced next key, and tokens name their key by `kid`. The accepted `alg` is an explicit allow list in `mowdeck-auth`. TLS versions and cipher suites at the edge are those of the operator's proxy or ingress. Keys, secrets and certificates are never compiled in: they come from the environment or secret files and can be rotated without a rebuild.

## Network encryption

The edge (browser and apps to the gateway) uses TLS terminated by the operator's proxy, Tailscale or the ingress. Passkeys, Web Push and the PWA need it, and the documentation says so ([Compose, HTTPS](../ops/compose.md)). AI providers are called over HTTPS. Vendor clouds are reached through the vendor libraries and the vendors' own endpoints. Nothing in the repository turns certificate verification off.

Inside the stack, service-to-service HTTP, NATS and PostgreSQL are not encrypted in the Compose setup. In the Helm chart, an external PostgreSQL can use TLS through the connection parameters (`ssl=require`). The hops are authenticated (tokens, per-service NATS users, database passwords, the OpenFGA key), but someone who can sniff the container network could read their traffic. See the residual risks; TLS inside the stack is on the [roadmap](https://github.com/lukislp/mowdeck/blob/main/ROADMAP.md#security-and-supply-chain).

## Supply chain

`main` only changes through pull requests that pass the required checks (python, web, docs, compose, helm, docker, e2e, e2e-web, camera-relay-media, openfga-model, dependency review, Socket) and the CodeQL gate. Commits must be signed. Dependencies are locked (`uv.lock`, `pnpm-lock.yaml`), actions are pinned to commit SHAs, third-party images are pinned in one list and mirrored. Bots go through the same gate. Updates of the mower vendor libraries need a hardware test before the merge ([vendor library updates](../ops/vendor-lib-updates.md)). Releases are produced only by the release workflow and signed keyless. The verification steps are in [SECURITY.md](https://github.com/lukislp/mowdeck/blob/main/SECURITY.md#verifying-releases).

## Residual risks (accepted)

| Risk | Why accepted | Mitigation |
| --- | --- | --- |
| No TLS between services, NATS and PostgreSQL inside the Compose network or cluster | One host or one cluster network; TLS inside needs certificates for every service | Authenticated hops, per-service NATS permissions, the stack is not exposed beyond the gateway; on the roadmap |
| No NetworkPolicies in the Helm chart | Not yet built | Every service authenticates its callers itself; on the roadmap |
| One shared password for the service database roles in Compose | Development default ([risks and technical debt](../architecture/11-risks-and-technical-debt.md)) | Databases reachable only inside the Compose network; on the roadmap |
| Traefik mounts the Docker socket read-only (Compose) | Needed for its Docker provider | Read-only mount, Traefik is a pinned upstream image |
| A revoked session's access token stays valid until it expires | Services verify tokens locally, without a call to Identity | Access tokens live 10 minutes; a revocation event to every service is on the roadmap ([ADR 0065](../adr/0065-security-center-and-new-login-alerts.md)) |
| Vendor clouds and vendor libraries | Outside the project's control | Credentials encrypted at rest, every vendor frame parsed and fuzzed, library updates only after a hardware test |
| Single maintainer (bus factor 1) | No second person available | [GOVERNANCE.md](https://github.com/lukislp/mowdeck/blob/main/GOVERNANCE.md#continuity-what-happens-if-the-maintainer-is-unavailable), fully public build |

## Review record

| Date | Scope | By | Outcome |
| --- | --- | --- | --- |
| 2026 (phase gates 1 to 6) | OWASP ASVS chapters per phase gate ([phases](../phases.md)), the security ADRs (0015, 0021, 0057, 0064, 0065, 0074) | Maintainer (internal) | Findings fixed with regression tests, decisions recorded in the ADRs |
| 2026-11-04 | This assurance case, the OpenSSF Best Practices review: development keys, TLS verification, SSRF paths, token handling | Maintainer (internal) | Identity now refuses its development key-encryption key in production. No other finding. Residual risks listed above |

No external security review has taken place so far. The next internal review is due when a trust boundary changes, and at the latest after twelve months.
