# Generated architecture diagrams

!!! note "Generated, do not edit"
    This page is written by `tools/repo-checks/scripts/arch_diagrams.py` from the code
    and the Compose file. Run `uv run poe arch-diagrams` after changing a service, a
    route, a NATS subject or the Compose stack; CI fails when the page is stale.
    The hand-written diagrams of the
    [architecture chapters](01-introduction-and-goals.md) and the ADRs explain the *why*;
    this page shows what the code does *today*.

## How this is generated

| Diagrams | Source of truth |
|---|---|
| Routing, platform, external systems, data ownership | `infra/compose/docker-compose.yml`: services, profiles, Traefik labels, `MOWDECK_*` environment variables, Alembic revisions of `services/*` |
| Messaging | `subscribe`, `publish` and `request` calls on the bus in `services/*/src`, subject constants and helpers of `libs/*/src` (read with `ast`), stream definitions in `mowdeck_contracts/v1/subjects.py` |
| HTTP API | OpenAPI documents in `apps/web/openapi` (exported from the services) |

Not derivable from code and therefore not shown: the browser, mobile and Home Assistant clients are drawn as one node; subjects built at runtime are listed as such.

## Services and routing

Traefik is the only entry point. Dotted arrows are services that start only with a Compose profile.

```mermaid
flowchart LR
  clients(["Browser, mobile apps,<br/>Home Assistant integration"])
  traefik{{"Traefik<br/>gateway"}}
  clients --> traefik
  subgraph sg_always["Always on"]
    device_gateway["device-gateway"]
    entitlements["entitlements"]
    fleet["fleet"]
    identity["identity"]
    notifications["notifications"]
    planner["planner"]
    realtime["realtime"]
    scheduler["scheduler"]
    telemetry["telemetry"]
    vision["vision"]
    web["web"]
  end
  subgraph sg_billing["Profile billing (opt-in)"]
    billing["billing"]
  end
  subgraph sg_demo["Profile demo (opt-in)"]
    whoami["whoami"]
  end
  subgraph sg_relay["Profile relay (opt-in)"]
    mediamtx["mediamtx"]
  end
  traefik -->|"/api/v1/device-gateway"| device_gateway
  traefik -->|"/api/v1/entitlements"| entitlements
  traefik -->|"/api/v1/fleet"| fleet
  traefik -->|"/api/v1/identity<br/>/api/v1/identity/session/<br/>+14 exact paths"| identity
  traefik -->|"/api/v1/notifications"| notifications
  traefik -->|"/api/v1/planner"| planner
  traefik -->|"/api/v1/realtime"| realtime
  traefik -->|"/api/v1/scheduler"| scheduler
  traefik -->|"/api/v1/telemetry"| telemetry
  traefik -->|"/api/v1/vision"| vision
  traefik -->|"/"| web
  traefik -.->|"/api/v1/billing<br/>+1 exact path"| billing
  traefik -.->|"/api/v1/whoami"| whoami
  traefik -.->|"/media/whep/r/"| mediamtx
```

| Service | Path prefixes | Exact paths | Profile |
|---|---|---|---|
| `billing` | `/api/v1/billing` | `/api/v1/billing/webhooks/stripe` | billing |
| `device-gateway` | `/api/v1/device-gateway` | - | always |
| `entitlements` | `/api/v1/entitlements` | - | always |
| `fleet` | `/api/v1/fleet` | - | always |
| `identity` | `/api/v1/identity`, `/api/v1/identity/session/` | `/.well-known/jwks.json`, `/api/v1/identity/app-login/session`, `/api/v1/identity/app-login/start`, `/api/v1/identity/me/external-identities/apple/link`, `/api/v1/identity/me/external-identities/github/link`, `/api/v1/identity/oauth/apple/callback`, `/api/v1/identity/oauth/apple/start`, `/api/v1/identity/oauth/github/callback`, `/api/v1/identity/oauth/github/start`, `/api/v1/identity/passkeys/challenge`, `/api/v1/identity/passkeys/session`, `/api/v1/identity/register`, `/api/v1/identity/session`, `/api/v1/identity/token` | always |
| `mediamtx` | `/media/whep/r/` | - | relay |
| `notifications` | `/api/v1/notifications` | - | always |
| `planner` | `/api/v1/planner` | - | always |
| `realtime` | `/api/v1/realtime` | - | always |
| `scheduler` | `/api/v1/scheduler` | - | always |
| `telemetry` | `/api/v1/telemetry` | - | always |
| `vision` | `/api/v1/vision` | - | always |
| `web` | `/` | - | always |
| `whoami` | `/api/v1/whoami` | - | demo |

Infrastructure containers: `mediamtx`, `nats`, `openfga`, `postgres`, `traefik`, `whoami`. One-shot jobs (migrations, bootstrap): `authz-bootstrap`, `billing-migrate`, `entitlements-migrate`, `fleet-migrate`, `identity-migrate`, `notifications-migrate`, `openfga-migrate`, `planner-migrate`, `scheduler-migrate`, `telemetry-migrate`, `vision-migrate`.

## Platform dependencies

Services are grouped by what they depend on: a database, NATS, OpenFGA and the token keys of identity (JWKS).

```mermaid
flowchart LR
  subgraph g0["NATS"]
    camera_relay["camera-relay"]
  end
  subgraph g1["NATS + JWKS"]
    device_gateway["device-gateway"]
  end
  subgraph g2["NATS + OpenFGA + JWKS"]
    realtime["realtime"]
  end
  subgraph g3["PostgreSQL + NATS + OpenFGA"]
    identity["identity"]
  end
  subgraph g4["PostgreSQL + NATS + OpenFGA + JWKS"]
    billing["billing"]
    entitlements["entitlements"]
    fleet["fleet"]
    notifications["notifications"]
    planner["planner"]
    scheduler["scheduler"]
    telemetry["telemetry"]
    vision["vision"]
  end
  PostgreSQL[("PostgreSQL<br/>one database per service")]
  NATS{{"NATS JetStream<br/>events, commands, telemetry"}}
  OpenFGA["OpenFGA<br/>authorization"]
  JWKS(["identity<br/>/.well-known/jwks.json"])
  g0 --> NATS
  g1 --> NATS
  g1 --> JWKS
  g2 --> NATS
  g2 --> OpenFGA
  g2 --> JWKS
  g3 --> PostgreSQL
  g3 --> NATS
  g3 --> OpenFGA
  g4 --> PostgreSQL
  g4 --> NATS
  g4 --> OpenFGA
  g4 --> JWKS
```

Services that call other services over HTTP (configured by URL):

| Service | Calls |
|---|---|
| `billing` | `entitlements` |
| `entitlements` | `device-gateway`, `fleet`, `identity`, `notifications`, `planner`, `realtime`, `scheduler`, `telemetry`, `vision` |
| `identity` | `device-gateway`, `entitlements`, `fleet`, `notifications`, `planner`, `realtime`, `scheduler`, `telemetry`, `vision` |

## External systems

Recognised from the environment variables each service is configured with.

```mermaid
flowchart LR
  subgraph mowdeck[MowDeck]
    agora_bridge["agora-bridge"]
    billing["billing"]
    device_gateway["device-gateway"]
    identity["identity"]
    notifications["notifications"]
    scheduler["scheduler"]
    vision["vision"]
  end
  subgraph outside[External systems]
    x0(["Agora (live camera)"])
    x1(["Apple Push Notification service"])
    x2(["GitHub OAuth"])
    x3(["Mammotion cloud and mower links"])
    x4(["Sign in with Apple"])
    x5(["Stripe"])
    x6(["Vision model provider"])
    x7(["Weather provider (default Open-Meteo)"])
    x8(["Web Push services of the browsers"])
  end
  agora_bridge --> x0
  billing --> x5
  device_gateway --> x3
  identity --> x2
  identity --> x4
  notifications --> x1
  notifications --> x8
  scheduler --> x7
  vision --> x6
```

## Messaging

Streams are the JetStream streams of the contracts; every other subject is core NATS request/reply. Subjects use `*` for the mower id.

### JetStream streams

#### CAMERA

Captures `camera.>`.

```mermaid
flowchart LR
  s_CAMERA{{"CAMERA"}}
  p_camera_relay["camera-relay"]
  c_fleet["fleet"]
  p_camera_relay -->|"camera.mower.*.relay.*"| s_CAMERA
  s_CAMERA -->|"camera.mower.*.relay.*"| c_fleet
```

#### COMMANDS

Captures `command.>`.

```mermaid
flowchart LR
  s_COMMANDS{{"COMMANDS"}}
  p_device_gateway["device-gateway"]
  p_fleet["fleet"]
  p_realtime["realtime"]
  p_vision["vision"]
  c_device_gateway["device-gateway"]
  c_fleet["fleet"]
  c_notifications["notifications"]
  c_realtime["realtime"]
  c_telemetry["telemetry"]
  c_vision["vision"]
  p_device_gateway -->|"command.result.mower.*"| s_COMMANDS
  p_fleet -->|"command.mower.*"| s_COMMANDS
  p_realtime -->|"command.mower.*"| s_COMMANDS
  p_vision -->|"command.mower.*"| s_COMMANDS
  s_COMMANDS -->|"command.mower.*"| c_device_gateway
  s_COMMANDS -->|"command.result.mower.*"| c_fleet
  s_COMMANDS -->|"command.mower.*"| c_notifications
  s_COMMANDS -->|"command.result.mower.*"| c_realtime
  s_COMMANDS -->|"command.result.mower.*"| c_telemetry
  s_COMMANDS -->|"command.mower.*<br/>command.result.mower.*"| c_vision
```

#### DEVICE

Captures `device.>`.

```mermaid
flowchart LR
  s_DEVICE{{"DEVICE"}}
  p_device_gateway["device-gateway"]
  c_notifications["notifications"]
  c_planner["planner"]
  c_realtime["realtime"]
  c_scheduler["scheduler"]
  c_telemetry["telemetry"]
  c_vision["vision"]
  p_device_gateway -->|"device.mower.*.capabilities<br/>device.mower.*.map<br/>device.mower.*.path"| s_DEVICE
  s_DEVICE -->|"device.mower.>"| c_notifications
  s_DEVICE -->|"device.mower.>"| c_planner
  s_DEVICE -->|"device.mower.*.capabilities"| c_realtime
  s_DEVICE -->|"device.mower.*.capabilities<br/>device.mower.>"| c_scheduler
  s_DEVICE -->|"device.mower.>"| c_telemetry
  s_DEVICE -->|"device.mower.>"| c_vision
```

#### EVENTS

Captures `event.>`.

```mermaid
flowchart LR
  s_EVENTS{{"EVENTS"}}
  p_billing["billing"]
  p_device_gateway["device-gateway"]
  p_entitlements["entitlements"]
  p_fleet["fleet"]
  p_identity["identity"]
  p_notifications["notifications"]
  p_scheduler["scheduler"]
  p_telemetry["telemetry"]
  p_vision["vision"]
  c_billing["billing"]
  c_device_gateway["device-gateway"]
  c_entitlements["entitlements"]
  c_fleet["fleet"]
  c_notifications["notifications"]
  c_realtime["realtime"]
  c_telemetry["telemetry"]
  c_vision["vision"]
  p_billing -->|"event.* (type chosen at runtime)"| s_EVENTS
  p_device_gateway -->|"event.* (type chosen at runtime)"| s_EVENTS
  p_entitlements -->|"event.* (type chosen at runtime)"| s_EVENTS
  p_fleet -->|"event.* (type chosen at runtime)"| s_EVENTS
  p_identity -->|"event.* (type chosen at runtime)<br/>event.identity.grants.changed"| s_EVENTS
  p_notifications -->|"event.* (type chosen at runtime)"| s_EVENTS
  p_scheduler -->|"event.* (type chosen at runtime)"| s_EVENTS
  p_telemetry -->|"event.* (type chosen at runtime)"| s_EVENTS
  p_vision -->|"event.* (type chosen at runtime)"| s_EVENTS
  s_EVENTS -->|"event.identity.account.created"| c_billing
  s_EVENTS -->|"event.mower.>"| c_device_gateway
  s_EVENTS -->|"event.identity.account.created"| c_entitlements
  s_EVENTS -->|"event.identity.account.created"| c_fleet
  s_EVENTS -->|"event.identity.><br/>event.mower.><br/>event.telemetry.><br/>+1 more"| c_notifications
  s_EVENTS -->|"event.identity.grants.changed<br/>event.notifications.><br/>event.vision.proposal.>"| c_realtime
  s_EVENTS -->|"event.mower.>"| c_telemetry
  s_EVENTS -->|"event.identity.>"| c_vision
```

#### TELEMETRY

Captures `telemetry.>`.

```mermaid
flowchart LR
  s_TELEMETRY{{"TELEMETRY"}}
  p_device_gateway["device-gateway"]
  c_camera_relay["camera-relay"]
  c_notifications["notifications"]
  c_realtime["realtime"]
  c_scheduler["scheduler"]
  c_telemetry["telemetry"]
  c_vision["vision"]
  p_device_gateway -->|"telemetry.mower.*"| s_TELEMETRY
  s_TELEMETRY -->|"telemetry.mower.*"| c_camera_relay
  s_TELEMETRY -->|"telemetry.mower.*"| c_notifications
  s_TELEMETRY -->|"telemetry.mower.*"| c_realtime
  s_TELEMETRY -->|"telemetry.mower.*"| c_scheduler
  s_TELEMETRY -->|"telemetry.mower.*"| c_telemetry
  s_TELEMETRY -->|"telemetry.mower.*"| c_vision
```

### Request and reply

#### Answered by camera-relay

```mermaid
flowchart LR
  r_camera_relay["camera-relay (answers)"]
  q_fleet["fleet"]
  q_fleet -->|"relay.camera.attach<br/>relay.camera.detach<br/>relay.camera.renew"| r_camera_relay
```

#### Answered by device-gateway

```mermaid
flowchart LR
  r_device_gateway["device-gateway (answers)"]
  q_camera_relay["camera-relay"]
  q_camera_relay -->|"gateway.mower.*.camera.release<br/>gateway.mower.*.camera.session"| r_device_gateway
  q_fleet["fleet"]
  q_fleet -->|"gateway.cloud.discover<br/>gateway.mower.*.bridge.scan<br/>gateway.mower.*.camera.session<br/>+3 more"| r_device_gateway
  q_notifications["notifications"]
  q_notifications -->|"gateway.mower.*.photo"| r_device_gateway
  q_vision["vision"]
  q_vision -->|"gateway.mower.*.photo"| r_device_gateway
```

#### Answered by entitlements

```mermaid
flowchart LR
  r_entitlements["entitlements (answers)"]
  q_billing["billing"]
  q_billing -->|"entitlements.plan.set"| r_entitlements
```

#### Answered by fleet

```mermaid
flowchart LR
  r_fleet["fleet (answers)"]
  q_device_gateway["device-gateway"]
  q_device_gateway -->|"fleet.mowers.credentials<br/>fleet.mowers.list"| r_fleet
  q_notifications["notifications"]
  q_notifications -->|"fleet.mowers.list"| r_fleet
  q_realtime["realtime"]
  q_realtime -->|"fleet.drive.start<br/>fleet.drive.stop"| r_fleet
  q_telemetry["telemetry"]
  q_telemetry -->|"fleet.mowers.list"| r_fleet
  q_vision["vision"]
  q_vision -->|"fleet.commands.issue<br/>fleet.drive.start<br/>fleet.drive.stop"| r_fleet
```

#### Answered by telemetry

```mermaid
flowchart LR
  r_telemetry["telemetry (answers)"]
  q_planner["planner"]
  q_planner -->|"tracks.positions.query"| r_telemetry
```

### All subjects

| Subject | Stream | Publishes or requests | Subscribes or answers |
|---|---|---|---|
| `camera.mower.*.relay.*` | CAMERA | camera-relay | fleet |
| `command.mower.*` | COMMANDS | fleet, realtime, vision | device-gateway, notifications, vision |
| `command.result.mower.*` | COMMANDS | device-gateway | fleet, realtime, telemetry, vision |
| `device.mower.*.capabilities` | DEVICE | device-gateway | realtime, scheduler |
| `device.mower.*.map` | DEVICE | device-gateway | - |
| `device.mower.*.path` | DEVICE | device-gateway | - |
| `device.mower.>` | DEVICE | - | notifications, planner, scheduler, telemetry, vision |
| `entitlements.check` | core | - | entitlements |
| `entitlements.plan.set` | core | billing | entitlements |
| `event.* (type chosen at runtime)` | EVENTS | billing, device-gateway, entitlements, fleet, identity, notifications, scheduler, telemetry, vision | - |
| `event.identity.>` | EVENTS | - | notifications, vision |
| `event.identity.account.created` | EVENTS | - | billing, entitlements, fleet |
| `event.identity.grants.changed` | EVENTS | identity | realtime |
| `event.mower.>` | EVENTS | - | device-gateway, notifications, telemetry |
| `event.notifications.>` | EVENTS | - | realtime |
| `event.telemetry.>` | EVENTS | - | notifications |
| `event.vision.>` | EVENTS | - | notifications |
| `event.vision.proposal.>` | EVENTS | - | realtime |
| `fleet.commands.issue` | core | vision | fleet |
| `fleet.drive.start` | core | realtime, vision | fleet |
| `fleet.drive.stop` | core | realtime, vision | fleet |
| `fleet.mowers.credentials` | core | device-gateway | fleet |
| `fleet.mowers.list` | core | device-gateway, notifications, telemetry | fleet |
| `fleet.targets.resolve` | core | - | fleet |
| `gateway.cloud.discover` | core | fleet | device-gateway |
| `gateway.mower.*.bridge.scan` | core | fleet | device-gateway |
| `gateway.mower.*.camera.release` | core | camera-relay | device-gateway |
| `gateway.mower.*.camera.session` | core | camera-relay, fleet | device-gateway |
| `gateway.mower.*.firmware.check` | core | fleet | device-gateway |
| `gateway.mower.*.path.preview` | core | fleet | device-gateway |
| `gateway.mower.*.photo` | core | fleet, notifications, vision | device-gateway |
| `relay.camera.attach` | core | fleet | camera-relay |
| `relay.camera.detach` | core | fleet | camera-relay |
| `relay.camera.renew` | core | fleet | camera-relay |
| `telemetry.mower.*` | TELEMETRY | device-gateway | camera-relay, notifications, realtime, scheduler, telemetry, vision |
| `tracks.positions.query` | core | planner | telemetry |

2 bus calls build their subject at runtime from variables and are not listed. Shared libraries (for example the plan-change subscription of the service kit) are not attributed to a service.

## HTTP API

Operations per OpenAPI tag of each service, from the documents the web app's types are generated from.

```mermaid
flowchart LR
  subgraph billing["billing"]
    direction TB
    billing_billing["billing<br/>6 operations"]
  end
  subgraph device_gateway["device-gateway"]
    direction TB
    device_gateway_device_gateway["device-gateway<br/>1 operation"]
  end
  subgraph entitlements["entitlements"]
    direction TB
    entitlements_entitlements["entitlements<br/>8 operations"]
  end
  subgraph fleet["fleet"]
    direction TB
    fleet_base_stations["base-stations<br/>8 operations"]
    fleet_linked_accounts["linked-accounts<br/>7 operations"]
    fleet_mowers["mowers<br/>28 operations"]
    fleet_sites_and_groups["sites and groups<br/>12 operations"]
  end
  subgraph identity["identity"]
    direction TB
    identity_identity__accounts["identity: accounts<br/>28 operations"]
    identity_identity__keys["identity: keys<br/>1 operation"]
    identity_identity__login_providers["identity: login providers<br/>5 operations"]
    identity_identity__passkeys["identity: passkeys<br/>11 operations"]
    identity_identity__platform["identity: platform<br/>1 operation"]
    identity_identity__sessions["identity: sessions<br/>15 operations"]
  end
  subgraph notifications["notifications"]
    direction TB
    notifications_notifications["notifications<br/>18 operations"]
  end
  subgraph planner["planner"]
    direction TB
    planner_planner["planner<br/>8 operations"]
  end
  subgraph realtime["realtime"]
    direction TB
  end
  subgraph scheduler["scheduler"]
    direction TB
    scheduler_schedules["schedules<br/>10 operations"]
  end
  subgraph telemetry["telemetry"]
    direction TB
    telemetry_telemetry["telemetry<br/>17 operations"]
  end
  subgraph vision["vision"]
    direction TB
    vision_vision["vision<br/>16 operations"]
  end
```

| Service | Tag | Operations | Paths |
|---|---|---|---|
| `billing` | `billing` | 6 | 6 |
| `billing` | `operations` | 1 | 1 |
| `device-gateway` | `device-gateway` | 1 | 1 |
| `device-gateway` | `operations` | 1 | 1 |
| `entitlements` | `entitlements` | 8 | 7 |
| `entitlements` | `operations` | 1 | 1 |
| `fleet` | `base-stations` | 8 | 5 |
| `fleet` | `linked-accounts` | 7 | 5 |
| `fleet` | `mowers` | 28 | 21 |
| `fleet` | `operations` | 1 | 1 |
| `fleet` | `sites and groups` | 12 | 6 |
| `identity` | `identity: accounts` | 28 | 19 |
| `identity` | `identity: keys` | 1 | 1 |
| `identity` | `identity: login providers` | 5 | 5 |
| `identity` | `identity: passkeys` | 11 | 9 |
| `identity` | `identity: platform` | 1 | 1 |
| `identity` | `identity: sessions` | 15 | 15 |
| `identity` | `operations` | 1 | 1 |
| `notifications` | `notifications` | 18 | 13 |
| `notifications` | `operations` | 1 | 1 |
| `planner` | `operations` | 1 | 1 |
| `planner` | `planner` | 8 | 6 |
| `realtime` | `operations` | 1 | 1 |
| `scheduler` | `operations` | 1 | 1 |
| `scheduler` | `schedules` | 10 | 7 |
| `telemetry` | `operations` | 1 | 1 |
| `telemetry` | `telemetry` | 17 | 17 |
| `vision` | `operations` | 1 | 1 |
| `vision` | `vision` | 16 | 13 |

The tag `operations` holds the probe endpoints every service exposes.

## Data ownership

Each service owns one database; nothing else reads it.

```mermaid
flowchart LR
  subgraph apps[Services]
    billing["billing"]
    entitlements["entitlements"]
    fleet["fleet"]
    identity["identity"]
    notifications["notifications"]
    planner["planner"]
    scheduler["scheduler"]
    telemetry["telemetry"]
    vision["vision"]
  end
  subgraph pg[PostgreSQL]
    db_billing[("billing<br/>2 migrations")]
    db_entitlements[("entitlements<br/>2 migrations")]
    db_fleet[("fleet<br/>12 migrations")]
    db_identity[("identity<br/>5 migrations")]
    db_notifications[("notifications<br/>7 migrations")]
    db_planner[("planner<br/>2 migrations")]
    db_scheduler[("scheduler<br/>3 migrations")]
    db_telemetry[("telemetry<br/>10 migrations")]
    db_vision[("vision<br/>7 migrations")]
  end
  billing -->|owns| db_billing
  entitlements -->|owns| db_entitlements
  fleet -->|owns| db_fleet
  identity -->|owns| db_identity
  notifications -->|owns| db_notifications
  planner -->|owns| db_planner
  scheduler -->|owns| db_scheduler
  telemetry -->|owns| db_telemetry
  vision -->|owns| db_vision
```

Services without a database: `agora-bridge`, `camera-relay`, `device-gateway`, `realtime`, `web`.
