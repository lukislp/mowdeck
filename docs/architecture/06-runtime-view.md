# 6. Runtime view

## Communication rules

- **From outside:** clients talk only to the gateway (REST and WebSocket). The public API is versioned (`/api/v1`).
- **Between services:** NATS JetStream. Commands as `command.mower.<id>`, state as `telemetry.mower.<id>`, domain events such as `mower.registered` or `subscription.changed`.
- **Tokens:** Identity issues short-lived JWTs (EdDSA, 10 minutes) with user, account, roles, permission keys and plan. Every service verifies them itself against Identity's published keys (`mowdeck-auth`, [ADR 0015](../adr/0015-tokens-sessions-and-key-rotation.md)).
- **Reliability:** commands have a unique ID, an expiry and emergency stop has priority.
- **Bridge:** the ESP32 bridge connects outbound (TLS, device secret), so it works behind any router.

## Scenario: send a start command

Implemented in Phase 1 by Fleet and the Device-Gateway ([ADR 0016](../adr/0016-fleet-commands-and-authorization-per-command-kind.md), [ADR 0013](../adr/0013-device-gateway-registry-and-sharding.md)). The relation Fleet checks depends on the command kind: `can_control` for start, pause, resume, dock, blade height and speed, `can_drive` for manual drive, `can_emergency_stop` (equal to `can_view`) for an emergency stop.

```mermaid
sequenceDiagram
    actor U as User (Operator)
    participant GW as Gateway (Traefik)
    participant F as Fleet
    participant A as OpenFGA
    participant DB as Fleet DB
    participant N as NATS JetStream
    participant D as Device-Gateway (shard i)
    participant C as MowerController
    participant S as SimulatedMower
    U->>GW: POST /api/v1/fleet/mowers/{id}/commands {command: start_mow} (JWT)
    GW->>F: forward
    F->>F: verify JWT locally, account from the token, relation = can_control
    F->>A: batch check [can_control, can_view] on mower:{id}
    alt neither
        F-->>U: 404 (nothing written, nothing published)
    else view only
        F-->>U: 403 (nothing written, nothing published)
    end
    F->>DB: load mower (account filter), insert command record (pending)
    F->>N: publish command.mower.{id} (msg id = command id, expiry, priority)
    F-->>U: 202 CommandAccepted (command id, status_url)
    N->>D: deliver (durable consumer device-gateway-i)
    D->>D: shard owns mower? account matches registration?
    D->>C: handle(envelope) in the mower's own task
    C->>C: expiry, de-duplication, queue or emergency priority
    C->>S: execute(StartMow)
    S-->>C: Outcome executed
    D->>N: command.result.mower.{id} (accepted, then executed)
    N-->>F: durable fleet-command-results: record status moves forward only
    U->>GW: GET /api/v1/fleet/commands/{command id}
    GW->>F: forward
    F-->>U: CommandView (status executed)
    loop every simulation tick, heartbeat every 10 s
        D->>N: telemetry.mower.{id} (on change or heartbeat)
    end
```

If the envelope's account differs from the registered owner, the Device-Gateway publishes a `rejected` result and the command never reaches the controller. Commands of different mowers run concurrently; an emergency stop overtakes queued commands of its own mower. A bulk command (`POST /groups/{id}/commands`, `/sites/{id}/commands`) needs `can_bulk_command` on the target (view for an emergency stop) and is then checked per mower: allowed mowers get their own command, visible ones without the right are reported `forbidden`, invisible ones are omitted.

## Scenario: register a mower

```mermaid
sequenceDiagram
    actor O as Owner
    participant F as Fleet
    participant A as OpenFGA
    participant DB as Fleet DB
    participant N as NATS JetStream
    participant D as Device-Gateway
    Note over F,D: start-up: D subscribes to event.mower.*, then requests fleet.mowers.list<br/>(queue group fleet, one replica answers with the shard's registrations)
    O->>F: POST /api/v1/fleet/mowers {name, connection_kind: simulated}
    F->>A: check can_manage_mowers on the account (default site) or the given site and groups
    F->>DB: begin: default site if missing, mower row, group memberships
    F->>A: write mower#account, mower#parent@site (and @group) (TupleWriter owner fleet)
    alt OpenFGA unavailable
        F->>DB: rollback
        F-->>O: 503, nothing changed, no event
    else written
        F->>DB: commit
        F->>N: event.mower.registered (MowerRegistration)
        F-->>O: 201 mower
        N-->>D: event.mower.registered
        D->>D: shard owns it? connect, start controller
        D->>N: event.mower.connected, telemetry.mower.{id}
    end
```

`identity.account.created` makes Fleet create the account's hidden default site; registering a mower without a site creates it too if the event has not arrived yet.

## Scenario: live status

Implemented by Telemetry ([ADR 0017](../adr/0017-telemetry-storage-and-retention.md)) and Realtime ([ADR 0018](../adr/0018-realtime-websocket-protocol.md)). Telemetry stores every state per the write rules and answers `GET /api/v1/telemetry/mowers/{id}/state` for clients that do not need a push; Realtime pushes states and command results over a WebSocket. Every Realtime instance subscribes to the whole stream (no queue group), so any instance can serve any client. The Phase 1 acceptance test (`tests/e2e`) runs this sequence through Traefik.

```mermaid
sequenceDiagram
    autonumber
    actor U as Anna (Operator, browser)
    participant R as Realtime
    participant A as OpenFGA
    participant F as Fleet
    participant N as NATS JetStream
    participant D as Device-Gateway
    participant T as Telemetry
    U->>R: GET /api/v1/realtime/ws (upgrade through Traefik)
    U->>R: {"type": "auth", "token": access token}
    R->>R: verify locally (JWKS), limits per user and account, expiry timer
    R-->>U: ready {principal, token_expires_at}
    U->>R: subscribe {mower_ids}
    R->>A: check_many(can_view, mower:<id>)
    R-->>U: subscribed {mower_ids, denied}, then the cached newest state
    U->>F: POST /api/v1/fleet/mowers/{id}/commands start_mow
    F->>A: can_control on the mower
    F->>N: command.mower.{id}
    F-->>U: 202 {command_id}
    N->>D: command
    D->>N: command.result.mower.{id} executed, telemetry.mower.{id} mowing
    N->>R: result and state (every instance, ephemeral consumers)
    R-->>U: command_result, state (newest state per mower, never blocks the bus)
    N->>T: state (durable queue consumer)
    T->>T: latest row, history (changes and a periodic row), position track
    Note over R,A: identity.grants.changed, every 60 s and on a new auth: re-check, revoked {mower_ids}
    Note over U,R: token exp without a new auth: close 4401; slow reader: close 4430
```

## Scenario: mowing finished notification

Implemented by Notifications ([ADR 0022](../adr/0022-notifications-rules-and-delivery.md)). The rules run per state under a row lock on the mower's watch; recipients are the subscribers of the account, re-checked with `can_view` before anything is stored; the outbox worker of any replica sends.

```mermaid
sequenceDiagram
    autonumber
    actor L as Lukas (browser)
    participant No as Notifications
    participant A as OpenFGA
    participant N as NATS JetStream
    participant D as Device-Gateway
    participant P as Push service / ntfy
    L->>No: PUT /api/v1/notifications/preferences (kinds, all mowers)
    L->>No: POST /api/v1/notifications/push-subscriptions (PushSubscription JSON)
    D->>N: telemetry.mower.{id} mowing ... charging
    N->>No: state (durable queue consumer notifications-states)
    No->>No: BEGIN, watch FOR UPDATE, rules: mowing_finished, COMMIT
    No->>No: subscribers of the account with the kind enabled
    No->>A: check_many(can_view, mower:<id>) for all subscribers
    No->>No: per recipient: render DE/EN, inbox item (unique dedupe_key), deliveries (quiet hours: none)
    No->>N: event.notifications.sent
    loop delivery worker on every replica
        No->>No: claim due deliveries FOR UPDATE SKIP LOCKED, attempts + 1, lease
        No->>P: Web Push (VAPID, aes128gcm, TTL, Urgency) or ntfy JSON publish
        P-->>No: 201 sent, 410 gone (subscription deleted), 429 or 5xx retry with backoff
    end
    L->>No: GET /api/v1/notifications/inbox, POST /inbox/{id}/read
```

## Scenario: a scheduled run

Implemented by the Scheduler ([ADR 0024](../adr/0024-scheduler-recurrence-rain-pause-and-command-issuing.md)). Every replica ticks; a due schedule is claimed with `FOR UPDATE SKIP LOCKED` and fired inside its transaction. The command goes through Fleet on behalf of the schedule's owner, so Fleet keeps the single command record and audit.

```mermaid
sequenceDiagram
    autonumber
    actor L as Lukas (browser)
    participant S as Scheduler
    participant A as OpenFGA
    participant F as Fleet
    participant W as Open-Meteo
    participant N as NATS JetStream
    participant D as Device-Gateway
    L->>S: POST /api/v1/scheduler/schedules (target mower, weekly, 10:30, rain pause)
    S->>A: can_edit_schedule and can_control on mower:<id>
    S->>F: fleet.targets.resolve (core NATS): site time zone and coordinates
    S->>S: next_run_at in the site's zone, owner = Lukas
    D->>N: telemetry.mower.{id}, device.mower.{id}.map
    N->>S: latest activity, map origin and areas (durable queue consumers)
    loop tick every 30 s on every replica
        S->>S: BEGIN, claim due schedule FOR UPDATE SKIP LOCKED
        S->>F: fleet.targets.resolve
        S->>A: owner still has can_control on mower:<id>? (no: skipped_permission)
        S->>S: mower busy? (yes: skipped_mower_busy)
        S->>W: hourly precipitation (cached 30 min per location)
        S->>S: rain decision (hold: skipped_rain)
        S->>F: fleet.commands.issue (on behalf of Lukas, start_mow, origin scheduler)
        F->>A: can_control for Lukas on mower:<id>
        F->>F: command record (origin, schedule_id)
        F->>N: command.mower.{id}
        F-->>S: IssueCommandReply(accepted)
        S->>S: schedule_runs row (started), next_run_at, COMMIT
        S->>N: event.scheduler.run.recorded
    end
    N->>D: command, executes, publishes results and states
    opt it starts to rain while the scheduled job mows
        S->>F: fleet.commands.issue (return_to_dock, on behalf of Lukas)
    end
```

## Scenario: a subscription through Stripe

Implemented by Billing ([ADR 0031](../adr/0031-billing-with-stripe-adapter.md)), only where billing is enabled (Compose profile `billing`). Payment pages are Stripe's; Billing only sees ids and statuses.

```mermaid
sequenceDiagram
    autonumber
    actor L as Lukas (owner, browser)
    participant B as Billing
    participant A as OpenFGA
    participant S as Stripe
    participant E as Entitlements
    participant N as NATS
    L->>B: GET /api/v1/billing/status (public: plans and prices)
    L->>B: POST /api/v1/billing/checkout {plan: home}
    B->>A: check can_manage_billing (and can_view) on account:<acc>
    B->>S: create customer (metadata account_id), Checkout Session (subscription, trial)
    B-->>L: {url}
    L->>S: pay on the hosted Checkout page
    S->>B: POST /webhooks/stripe checkout.session.completed, customer.subscription.created
    B->>B: verify Stripe-Signature, record and claim the event id
    B->>N: entitlements.plan.set {account, home, trial, trial_until}
    N->>E: request (queue entitlements)
    E->>E: account_plans source=billing, history row
    E->>N: event.entitlements.plan.changed
    E-->>B: PlanSetReply ok
    B->>N: event.billing.subscription.changed
    B-->>S: 200 (a repeated delivery: 200 duplicate)
    S->>B: later customer.subscription.updated (past_due)
    B->>N: entitlements.plan.set {grace, grace_until = paid period end + 14 days}
```

## Scenario: AI assist frees a stuck mower

Implemented by the Vision service ([ADR 0033](../adr/0033-vision-assisted-recovery-with-approval.md)) on top of the drive channel of [ADR 0026](../adr/0026-manual-drive-channel.md). The Device-Gateway's controller stays the safety authority.

```mermaid
sequenceDiagram
    autonumber
    actor A as Anna (owner, phone)
    participant V as Vision
    participant M as Vision model (LiteLLM)
    participant N as NATS
    participant NO as Notifications
    participant F as Fleet
    participant D as Device-Gateway
    Note over V,N: telemetry.mower.* and device.mower.*.map (newest per mower) feed the view
    A->>V: POST /vision/mowers/{id}/recover {task, snapshot}
    V->>V: can_drive, ai_recover, provider (own, else hosted), daily caps
    opt no snapshot from the app
        V->>D: gateway.mower.{id}.photo (core NATS)
        D-->>V: image (capture_photo)
    end
    V-->>A: 202 proposal (analysing)
    V->>M: images + scene context (mower frame, dock bearing), JSON schema
    M-->>V: situation, confidence, steps, risk
    alt risk flag or low confidence or no step
        V->>V: rejected_by_ai (never executed)
    else
        V->>V: proposed (expires after 5 min)
        V->>N: event.vision.proposal.created
        N->>NO: vision_proposal to users with can_drive (push, inbox, deep link)
    end
    A->>V: POST /proposals/{id}/approve
    V->>F: fleet.drive.start (on behalf of Anna, origin vision, blades off)
    F-->>V: session
    loop 5 frames per second per step
        V->>N: command.mower.{id} ManualDrive (ttl_ms 1000, blade_on false)
        N->>D: controller: dead-man timer, blade rules, drive_recoverable check
        D->>N: telemetry, results
        N->>V: abort on emergency stop, sensor stop, geofence, no state, cancel
    end
    V->>N: zero frame (unless emergency or sensor stop)
    V->>F: fleet.drive.stop (completed or aborted)
    V->>V: verify after 1.5 s: recovered or not_recovered (retry up to 3 rounds)
    opt find_dock succeeded
        V->>F: fleet.commands.issue return_to_dock (origin vision)
    end
```

## Scenario: manual driving with the joystick

Implemented by Realtime and Fleet ([ADR 0026](../adr/0026-manual-drive-channel.md)); the Device-Gateway's controller stays the only safety authority ([ADR 0012](../adr/0012-mower-control-interface-and-safety-rules.md)). The session is opened once through Fleet; the frames go from Realtime straight to the gateway.

```mermaid
sequenceDiagram
    autonumber
    actor A as Anna (Operator, browser)
    participant R as Realtime
    participant O as OpenFGA
    participant F as Fleet
    participant N as NATS JetStream
    participant D as Device-Gateway
    Note over R,N: device.mower.*.capabilities (newest per mower) fills the capability cache
    A->>R: drive_start {mower_id, blade_on_allowed}
    R->>O: can_drive on mower:<id> (no: drive_not_allowed)
    R->>R: capabilities: manual_drive? (no or unknown: drive_unsupported)
    R->>F: fleet.drive.start (core NATS, on behalf of Anna)
    F->>O: can_drive for Anna (404 semantics)
    F->>F: per-mower lock, no other open session (else busy), drive_sessions row
    F-->>R: DriveStartReply(started: session_id, account_id)
    R-->>A: drive_started {session_id, max_rate_hz 10, ttl_ms 1000, capabilities}
    loop about 10 per second while the stick is held
        A->>R: drive_frame {session_id, seq, linear, angular, blade_on}
        R->>R: token bucket (excess frames dropped and counted)
        R->>N: command.mower.{id} ManualDrive (ttl_ms 1000, correlation drive:<session>)
        R-->>A: drive_frame_ack {seq}
        N->>D: frame (account check, per-mower task)
        D->>D: controller: blade rules, re-arm dead-man timer, session.update
    end
    alt drive_stop, socket closed, revoked, token expiry, max duration, refused frame
        R->>N: zero frame (stop at once)
        R-->>A: drive_ended {reason} (if the socket is open)
        R->>F: fleet.drive.stop {reason, frames, frames_dropped}
    else frames just stop (lost network, closed laptop)
        D->>D: dead-man timer fires after ttl_ms: stop and close the drive session
    end
    Note over A,D: emergency stop stays on POST /fleet/mowers/{id}/commands and overtakes everything
```

## Scenario: live camera

Implemented by Fleet, the Device-Gateway and the web app ([ADR 0032](../adr/0032-live-camera-over-agora.md)). MowDeck hands out credentials only; the video goes from the mower through Mammotion's cloud and Agora straight to the browser.

```mermaid
sequenceDiagram
    autonumber
    actor A as Anna (Viewer, browser)
    participant F as Fleet
    participant O as OpenFGA
    participant E as Entitlements
    participant D as Device-Gateway
    participant M as Mammotion cloud
    participant G as Agora
    A->>F: POST /fleet/mowers/{id}/camera/session
    F->>O: can_view on mower:<id> (404 semantics)
    F->>E: feature camera (cached; no: 403 feature-not-in-plan)
    F->>D: gateway.mower.<id>.camera.session (core NATS, the shard's instance answers)
    D->>D: account owns the mower? connection CameraCapable?
    D->>M: get_stream_subscription (PyMammotion; the mower starts publishing)
    M-->>D: appid, channel, token, uid, cameras, key and salt
    D-->>F: CameraSessionReply(session)
    F->>F: audit mower.camera_opened (never the token)
    F-->>A: CameraSession (Cache-Control: no-store)
    A->>G: join channel as audience (Agora Web SDK, encryption first)
    G-->>A: video of the mower's camera
    loop a minute before expires_at
        A->>F: POST .../camera/session
        F-->>A: fresh session
        A->>G: renewToken
    end
    A->>G: leave (panel closed or page left)
```

## Scenario: registration and login

Implemented by Identity ([ADR 0015](../adr/0015-tokens-sessions-and-key-rotation.md)). The tuples are written inside the database transaction, right before the commit; if OpenFGA cannot be written, nothing is saved and the request answers 503.

```mermaid
sequenceDiagram
    autonumber
    actor U as Lukas (browser)
    participant T as Traefik
    participant I as Identity
    participant DB as Identity DB
    participant F as OpenFGA
    participant N as NATS
    U->>T: POST /api/v1/identity/register {username, password, account_name}
    T->>I: route (rate limited per client IP)
    I->>I: registration mode, password policy, Argon2id hash
    I->>DB: begin: account, user, owner membership, default roles, audit row
    I->>F: write account:A#owner, #member @ user:L, account:A#platform @ platform:main
    I->>DB: commit
    I->>N: event.identity.account.created, user.created, grants.changed
    I-->>U: 201 {account_id, user_id}
    U->>I: POST /api/v1/identity/session {username, password}
    I->>DB: failures of this username in the last 15 minutes (429 if too many)
    I->>DB: verify Argon2id hash, choose the account, create the session family, audit row
    I->>I: sign JWT (EdDSA, 10 min: sub, acc, kind, roles, perms, plan, sid)
    I-->>U: 200 {access_token, refresh_token} + HttpOnly refresh cookie
    U->>I: POST /api/v1/identity/session/refresh (every < 10 min)
    I->>DB: rotate the refresh token (a reused one revokes the family)
    I-->>U: 200 new access token with fresh claims
```

## Scenario: an authorized request

Every service verifies the token itself; Identity is not asked per request. Implemented by the Device-Gateway (token level), Identity and Fleet (object level).

```mermaid
sequenceDiagram
    autonumber
    actor A as Anna (app)
    participant T as Traefik
    participant S as Service (Fleet)
    participant J as JWKS cache (mowdeck-auth)
    participant Z as mowdeck-authz
    participant F as OpenFGA
    participant DB as Service DB
    A->>T: POST /api/v1/fleet/mowers/m1/commands (Bearer JWT)
    T->>S: route (no auth decision in the gateway)
    S->>J: key for kid (cached 5 min; an unknown kid refetches the JWKS once)
    S->>S: verify EdDSA signature, iss, aud, exp; Principal(acc=A)
    S->>Z: require_or_not_found(can_control, mower:m1)
    Z->>Z: pre-filter: "mower.control" in perms? decision cache
    Z->>F: batch check can_control and can_view for user:anna on mower:m1
    F-->>Z: [true, true]
    S->>DB: SELECT mower WHERE id = m1 AND account_id = A
    S-->>A: 202
    Note over S,F: Bob (account B): the path account or the check fails, the answer is 404
```

## Scenario: foreign account is rejected

A user of account B requests mower `X` of account A. The service takes the account ID from the token (B), the query filter and the OpenFGA check both fail, and the service answers 404 or 403 without revealing existence. Every service ships tests for exactly this case.
