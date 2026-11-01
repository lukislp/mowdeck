# ADR 0073: NATS authentication and per-service subject permissions

## Status

Accepted (2026-11-01). Follow-up of [ADR 0068](0068-public-demo-mode.md) (amendment of 2026-10-31: "authenticating the services to NATS, one credential per service, is a follow-up with its own ADR").

## Context

NATS is MowDeck's internal bus: commands to the mowers, telemetry, domain events, device snapshots and about thirty core NATS request subjects between the services (`fleet.mowers.credentials`, `gateway.mower.<id>.camera.session`, `entitlements.check`, ...). Until now the server accepted any client: whatever container reached it could publish and subscribe to everything. The only protection was the Docker network (and, since ADR 0068's amendment, that the demo publishes no NATS port). A compromised container (a service with a vulnerable dependency, a vendor library of the Device-Gateway) could therefore:

- send commands to every mower (`command.mower.*`), forge command results, telemetry or domain events of another service;
- read the vendor credentials Fleet hands the Device-Gateway (`fleet.mowers.credentials` replies, which travel on the requester's `_INBOX`);
- reconfigure or delete the JetStream streams, read any stream through a consumer with any filter.

Defense in depth asks for each service to be able to use only the subjects it needs, enforced by the server.

### Options

1. **One user and password per service in the server configuration**, with publish and subscribe allow lists (`authorization { users = [...] }`). The configuration is a plain file; passwords come from environment variables. Nothing new to run, every NATS client supports it (nats-py: `user`/`password`), works the same with Compose, Helm and an external NATS.
2. **One NKey per service** (ed25519 key pairs; the server knows only the public keys). No shared secret on the server. But nats-py needs the optional `nkeys` package, and the operator generates key pairs with the `nk` tool instead of a random string; the seed is a secret just like a password. The permission model is the same as in option 1.
3. **Decentralised JWT accounts** (operator, accounts, user JWTs signed by an account key, a resolver). The most flexible (revocation, per-account limits, exports/imports between accounts), and the most moving parts: an operator key hierarchy, `nsc`, a resolver in the server, JWT renewal. Built for multi-tenant NATS deployments, which a self-hosted MowDeck is not.

Token authentication (one token per service) is option 1 without user names; NATS permissions are attached to users, so it does not fit.

## Decision

**Option 1**: one NATS user per service (the user name is the Compose service name) plus the user `streams`, each with a password and least-privilege permissions. It is the simplest option that is robust in Compose and Helm, needs no new tool or dependency, and the permissions are as fine-grained as with NKeys or JWTs. Moving to NKeys later only changes the credential lines of the generated file and the connect call; the subject map stays.

### The subject map

`tools/repo-checks/scripts/nats_auth.py` holds the one reviewable map (`USERS`): per service the subjects it publishes on or requests (`publish`), the core NATS subjects it answers (`serve`) and the stream filters it consumes (`consume`). `uv run poe nats-auth` renders it into `infra/compose/nats/auth.conf` (and the identical chart copy `infra/helm/mowdeck/files/nats-auth.conf`); the generated permissions are least privilege by construction:

- **publish**: the listed subjects; `$JS.API.STREAM.INFO.<stream>` of the five streams; for each consumed filter `$JS.API.CONSUMER.CREATE.<stream>.*.<filter>`, plus `$JS.API.CONSUMER.INFO.<stream>.*` and `$JS.ACK.<stream>.>` of the consumed streams. Nothing else: no stream management, no legacy consumer API, no message get or delete.
- **subscribe**: the served subjects and the user's own inbox `_INBOX.<user>.>`. A service never sees another service's replies or deliveries.
- **allow_responses** for users that serve requests: they may answer once, to the requester's inbox, within two minutes, without a publish permission on `_INBOX.>`.
- **streams**: `$JS.API.STREAM.INFO|CREATE|UPDATE.<stream>` of the five streams, nothing else.

| User | Publishes and requests | Answers (core NATS) | Consumes (JetStream) |
| --- | --- | --- | --- |
| `billing` | `entitlements.plan.set`<br>`event.billing.>` | - | `event.identity.account.created`<br>`event.identity.grants.changed` |
| `camera-relay` | `camera.mower.*.relay.*`<br>`gateway.mower.*.camera.release`<br>`gateway.mower.*.camera.session` | `relay.camera.attach`<br>`relay.camera.detach`<br>`relay.camera.renew` | `telemetry.mower.*` |
| `device-gateway` | `command.result.mower.*`<br>`device.mower.*.capabilities`<br>`device.mower.*.map`<br>`device.mower.*.path`<br>`event.mower.connected`<br>`event.mower.disconnected`<br>`fleet.mowers.credentials`<br>`fleet.mowers.list`<br>`telemetry.mower.*` | `gateway.cloud.discover`<br>`gateway.cloud.providers`<br>`gateway.errors.entries`<br>`gateway.errors.explain`<br>`gateway.errors.lexicons`<br>`gateway.mower.*.bridge.scan`<br>`gateway.mower.*.camera.release`<br>`gateway.mower.*.camera.session`<br>`gateway.mower.*.firmware.check`<br>`gateway.mower.*.path.preview`<br>`gateway.mower.*.photo`<br>`gateway.mower.*.tracking` | `command.mower.*`<br>`event.mower.>` |
| `entitlements` | `event.entitlements.>` | `entitlements.check`<br>`entitlements.plan.set` | `event.identity.account.created`<br>`event.identity.grants.changed` |
| `fleet` | `command.mower.*`<br>`entitlements.check`<br>`event.fleet.>`<br>`event.mower.registered`<br>`event.mower.removed`<br>`event.mower.updated`<br>`gateway.cloud.discover`<br>`gateway.cloud.providers`<br>`gateway.mower.*.bridge.scan`<br>`gateway.mower.*.camera.session`<br>`gateway.mower.*.firmware.check`<br>`gateway.mower.*.path.preview`<br>`gateway.mower.*.photo`<br>`relay.camera.attach`<br>`relay.camera.detach`<br>`relay.camera.renew` | `fleet.commands.issue`<br>`fleet.drive.start`<br>`fleet.drive.stop`<br>`fleet.mowers.credentials`<br>`fleet.mowers.list`<br>`fleet.targets.resolve` | `camera.mower.*.relay.*`<br>`command.result.mower.*`<br>`event.entitlements.plan.changed`<br>`event.identity.account.created`<br>`event.identity.grants.changed` |
| `identity` | `entitlements.check`<br>`event.identity.>` | - | `event.entitlements.plan.changed`<br>`event.identity.grants.changed` |
| `notifications` | `entitlements.check`<br>`event.notifications.>`<br>`fleet.mowers.list`<br>`gateway.errors.explain`<br>`gateway.mower.*.photo` | - | `command.mower.*`<br>`device.mower.>`<br>`event.entitlements.plan.changed`<br>`event.identity.>`<br>`event.mower.>`<br>`event.telemetry.>`<br>`event.tracking.>`<br>`event.vision.>`<br>`telemetry.mower.*` |
| `planner` | `entitlements.check`<br>`tracks.positions.query` | - | `device.mower.>`<br>`event.entitlements.plan.changed`<br>`event.identity.grants.changed` |
| `realtime` | `command.mower.*`<br>`entitlements.check`<br>`fleet.drive.start`<br>`fleet.drive.stop` | - | `command.result.mower.*`<br>`device.mower.*.capabilities`<br>`event.entitlements.plan.changed`<br>`event.identity.grants.changed`<br>`event.notifications.>`<br>`event.vision.proposal.>`<br>`telemetry.mower.*` |
| `scheduler` | `entitlements.check`<br>`event.scheduler.>`<br>`fleet.commands.issue`<br>`fleet.targets.resolve` | - | `device.mower.>`<br>`event.entitlements.plan.changed`<br>`event.identity.grants.changed`<br>`telemetry.mower.*` |
| `streams` | creates and updates the streams COMMANDS, TELEMETRY, EVENTS, DEVICE, CAMERA | - | - |
| `telemetry` | `event.telemetry.>`<br>`event.tracking.>`<br>`fleet.mowers.list`<br>`gateway.errors.entries`<br>`gateway.errors.explain`<br>`gateway.errors.lexicons`<br>`gateway.mower.*.photo`<br>`gateway.mower.*.tracking` | `tracks.positions.query` | `command.result.mower.*`<br>`device.mower.>`<br>`event.mower.>`<br>`event.notifications.>`<br>`telemetry.mower.*` |
| `vision` | `command.mower.*`<br>`entitlements.check`<br>`event.vision.>`<br>`fleet.commands.issue`<br>`fleet.drive.start`<br>`fleet.drive.stop`<br>`gateway.mower.*.photo` | - | `command.mower.*`<br>`command.result.mower.*`<br>`device.mower.>`<br>`event.entitlements.plan.changed`<br>`event.identity.>`<br>`telemetry.mower.*` |

(`python tools/repo-checks/scripts/nats_auth.py --summary` prints this table from the map.) Every event domain has exactly one publisher; only Fleet, Realtime (manual drive frames) and Vision (AI drive frames) may send commands, only the Device-Gateway may publish telemetry, device snapshots and command results.

### Keeping the map in step with the code

`tools/repo-checks/tests/test_nats_auth.py` scans `services/*/src` with `ast` (reusing the constant evaluation of the architecture diagrams): every `subscribe`, `request` and `publish` on a bus with a resolvable subject, the event types a service emits (`work.emit(EVENT, ...)`, `events.publish(EVENT, ...)`, `EventEnvelope(type=EVENT)`, `{"type": EVENT}`), the bus helpers of the libraries (`publish_command`, `publish_command_result`, the Entitlements client, the authorization cache invalidation). It fails when the code uses a subject the map does not allow, and when the map allows a subject no code uses. It also checks that the generated files are fresh, that no two users publish the same event subject, that no user may read another user's inbox, use the legacy consumer API or change a stream (except `streams`), and that Compose gives every service its own user and only its own password. A test against a real `nats-server` with the generated file (`test_nats_auth_server.py`, CI job `python`) proves the server enforces it: no anonymous client, publishing outside the allow list fails, another user's inbox and broader consumer filters are refused, only `streams` may change a stream. The end-to-end jobs fail on any permission violation in the NATS log (`infra/compose/nats-violations.sh`), so least privilege cannot silently break a flow the suites exercise.

### Client side (mowdeck-service-kit)

- `MOWDECK_NATS_USER` and `MOWDECK_NATS_PASSWORD` (or `MOWDECK_NATS_PASSWORD_FILE`, a `SecretStr`, never logged); both or neither. Without them a service connects without credentials, as before (development, tests).
- The inbox prefix is `_INBOX.<user>` (nats-py `inbox_prefix`), so replies and deliveries stay inside the user's own subscribe permission.
- Stream consumers are created through the subject based API (`$JS.API.CONSUMER.CREATE.<stream>.<name>.<filter>`): the server checks that the filter in the request equals the one in the API subject, so the permission on the API subject limits what a service can read from a stream. Ephemeral consumers are named (`eph-<random>`) for that reason. A durable consumer delivers to the stable subject `_INBOX.<user>.durable.<stream>.<name>`, the same for every replica of a queue group.
- **Upgrade of existing durable consumers**: consumers created before this change deliver to a random `_INBOX.<nuid>`, which the service may no longer read. On subscribe the bus moves such a consumer to its own inbox (a consumer update of the delivery subject only), keeping its position, so no message is lost or delivered twice.
- **Streams**: with credentials a service no longer creates or updates the streams (`MOWDECK_NATS_MANAGE_STREAMS`, default: only without a user). The one-shot job `nats-streams` (`python -m mowdeck_service_kit.bus.streams`, in the identity image) does it as the user `streams` before the services start (Compose: `depends_on ... service_completed_successfully`; Helm: a Job per release revision, the services wait up to `MOWDECK_NATS_STREAM_WAIT_SECONDS`, default 120, for the streams).

### Passwords

One environment variable per user, `NATS_PASSWORD_<USER>` (`NATS_PASSWORD_DEVICE_GATEWAY`, ...). The NATS server reads all of them (the generated file references them as `$NATS_PASSWORD_<USER>`); each service receives only its own. The server parses a value from its environment as a configuration value, so a password starting with a digit could be read as a number: a password is 24 to 128 letters, digits, `-` and `_`, starting with a letter. `infra/compose/nats-passwords.sh <env-file>` generates the missing ones (`n` plus 48 random hex digits), replaces placeholders, keeps good values and never prints a value; `--check` only reports. The Helm chart takes them as `secrets.NATS_PASSWORD_*` (required and validated at render time) into the Secret `natsAuth.secretName`, or the operator creates that Secret (`natsAuth.createSecret=false`).

The passwords are kept in plain text in the NATS server's environment (the server logs a warning that suggests bcrypt or NKeys). Bcrypt hashes start with `$`, which the NATS configuration reads as a variable reference, and would need a second, hashed value per user in `.env`; the NATS container is as trusted as `.env` itself, which holds every password anyway. NKeys would remove the shared secret (option 2) and remain the upgrade path.

### Rollout: required at once, failing early

Authentication is on in every stack (no switch): an optional flag would leave most installations unprotected and double the configurations to test. The new variables are required (`${NATS_PASSWORD_<USER>:?...}` in Compose, `required` in Helm), so a missing password stops the update before anything changes, with a message that names the variable and the script:

- **Compose with `deploy.sh`** (the owner's server): the script of the running release performs the update, and it predates this change. `docker compose pull` (step 6) fails on the first missing password, after the checkout and before the maintenance window or any container restart; the running stack stays as it is. The owner runs `./nats-passwords.sh .env` and starts `deploy.sh` again. From this release on, `deploy.sh` itself checks the new release's NATS passwords right after verifying the images (step 4, `./nats-passwords.sh --check .env`), before `.env` or a container changes. `deploy.sh` does not generate the passwords itself: it never writes a secret into `.env`, and the owner keeps control of the file and its backups. Generating them is one command and safe to repeat.
- **Public demo** (`demo/reset.sh`): the reset refuses to run until `demo/demo.env` has good NATS passwords (`./nats-passwords.sh demo/demo.env`), like its other secrets. Every reset recreates NATS and its volumes anyway.
- **Helm**: `helm upgrade` fails to render until `secrets.NATS_PASSWORD_*` are given (or `natsAuth.createSecret=false` with an own Secret).
- **External NATS** (`nats.enabled=false`, `natsUrl`): the operator configures the users of `files/nats-auth.conf` in their own server.

A release that brings this change is a breaking change for operators (`feat!:`); the release notes carry the upgrade step.

## Consequences

- A compromised service can only publish, request and consume what its own code already does; it cannot command mowers (unless it is Fleet, Realtime or Vision), forge another service's events, read another service's replies (vendor credentials included) or change, purge or delete a stream.
- Adding a bus subject to a service now means adding it to `USERS` and running `uv run poe nats-auth`; the repository check names the missing entry. A new service needs its user, `NATS_PASSWORD_<SERVICE>` in Compose (`nats` and the service), `.env.example`, `demo/demo.env.example` and the chart's `secrets` (the service template's Compose snippet says so).
- Two more secrets mechanisms to operate: `NATS_PASSWORD_*` in `.env`/`demo.env`/Helm values. Rotation: change the value(s) and `docker compose up -d` (Compose recreates NATS and the affected services); in Kubernetes restart the NATS StatefulSet after changing the Secret (the service pods restart on their own through the secrets checksum).
- Residual risks, accepted: a service with a consumer on a stream filter may also create a consumer with another service's durable name on the same filter (and so take over its deliveries), because consumer names cannot be restricted per prefix; it can only read what it is allowed to read anyway. The NATS monitoring endpoint (port 8222) stays unauthenticated and bound to the container network (Compose: host loopback). The plaintext passwords in the NATS server's environment (see above).
- Follow-ups, not now: NKeys instead of passwords (option 2); per-service durable consumer names in the permissions if the take-over above ever matters; a system account user for operators who want the `nats` CLI against a production stack.
