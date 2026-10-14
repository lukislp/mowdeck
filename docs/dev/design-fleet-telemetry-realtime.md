# Design: Fleet, Telemetry, Realtime and the Phase 1 acceptance test (Tasks 7 to 9)

Status: approved 2026-10-09. **Section 1 (Fleet) is implemented** (Task 7, `services/fleet`, [ADR 0016](../adr/0016-fleet-commands-and-authorization-per-command-kind.md)); the corrections against this draft are listed in section 1.0 and win over the rest of the text. **Section 2 (Telemetry) is implemented** (Task 8, `services/telemetry`, [ADR 0017](../adr/0017-telemetry-storage-and-retention.md)); the corrections are listed in section 2.0 and win over the rest of section 2. **Sections 3 to 5 (Realtime, Compose wiring, acceptance test) are implemented** (Task 9, `services/realtime`, `tests/e2e`, [ADR 0018](../adr/0018-realtime-websocket-protocol.md)); the corrections are listed in section 3.0 and win over the rest of sections 3 to 5. Scope: the remaining Phase 1 services and the gate test of `docs/phases.md`.

Inputs (all on `main`): `docs/plan/project-plan.de.md` (sections "Dienste im Überblick", "Kommunikation & Verträge", "Konten, Benutzer & Rechte", "Flottenmanagement", "Funktionsumfang"), `docs/phases.md`, ADR 0009, 0012, 0013, 0014, `docs/dev/design-identity-authz.md` (cited as "identity design"), `docs/architecture/06-runtime-view.md`, `libs/mowdeck-contracts/src/mowdeck_contracts/v1/{fleet,envelopes,mower,subjects,principal}.py`, `services/device-gateway/README.md` and `src/device_gateway/application/gateway.py`, `libs/mowdeck-authz/README.md` and `src/mowdeck_authz/{fastapi,permissions}.py`, `libs/mowdeck-service-kit/README.md` and `src/mowdeck_service_kit/{app,bus/base,bus/jetstream}.py`, `templates/service/`, `docs/dev/service-template.md`, `infra/compose/docker-compose.yml`, `infra/compose/init/01-databases.sql`, `.github/workflows/ci.yml`, root `pyproject.toml`.

Markers: **[assumption]** is a choice made here that the owner or the Identity implementer (Task 6) should confirm; **[unverified]** is an external fact not checked against the source.

---

## 0. Key decisions at a glance

1. **Fleet is the only producer of commands and registrations.** It writes `site#account`, `group#account/parent`, `mower#account/parent` through `TupleWriter(owner="fleet")`, publishes `mower.registered|updated|removed`, answers `fleet.mowers.list` (queue group, all shards filtered in Python) and builds every `CommandEnvelope` from the verified principal.
2. **DB first, FGA and bus through an outbox.** *(Implemented differently: tuples are written inside the database transaction like Identity, events after the commit; see 1.0.)* A mower row, its tuples and its `mower.registered` event are committed as one transaction plus outbox rows, drained synchronously after commit and retried in the background, so a crash cannot leave a mower without tuples or without the event. Tuples are drained before the event, so the Device-Gateway never connects a mower that nobody can see yet.
3. **The required relation depends on the command kind**: `can_control` for start, pause, resume, dock, blade height, speed; `can_drive` for manual drive; `can_emergency_stop` (equals `can_view` in the model) for emergency stop. The check runs before anything touches the database or the bus; a refused command never reaches NATS (tested).
4. **Fleet keeps the authoritative command record** (`commands` table: who, what, when, latest status), updated from `command.result.mower.*` by a durable queue consumer. `GET /commands/{id}` reads it. This is also the command audit the plan asks for.
5. **The hidden default site is created on `identity.account.created` (subject `event.identity.account.created`) and lazily on first use**, with a partial unique index, so a mower registered seconds after registration never races the event.
6. **Telemetry stores latest state, a de-duplicated history, a position track and a command result timeline** in PostgreSQL (plain tables, `date_bin` downsampling, advisory-locked retention job). TimescaleDB stays an option behind the same queries.
7. **Realtime authenticates with the JWT in the first WebSocket message** (not query string, not subprotocol), checks every subscription with `check_many(can_view)`, re-checks on `authz.tuples_changed`, on a timer and on token refresh, and closes the socket when the token expires without refresh.
8. **One JetStream subscription per instance per subject family** (`telemetry.mower.*`, `command.result.mower.*`), a mower-to-connections index, conflation of `state` messages per mower and a bounded queue for everything else; a client that cannot keep up is closed with code 4430.
9. **The acceptance test lives in `tests/e2e/`**, outside the unit `testpaths`, runs with `uv run poe e2e` against `docker compose up --wait`, and needs Identity in open registration mode (two accounts).
10. **Task order: 7 Fleet, 8 Telemetry (can start in parallel with 7), 9 Realtime plus the e2e gate.** Traefik loses its `authz-bootstrap` dependency once Identity, Fleet, Telemetry and Realtime depend on it themselves.

---

## 1. Fleet service (Task 7)

Generated from `templates/service` with `needs_db=true`, package `fleet`, database and role `fleet` (already in `infra/compose/init/01-databases.sql`). Libraries: `mowdeck-service-kit[db]`, `mowdeck-contracts`, `mowdeck-authz[fastapi]`, `mowdeck-auth` (Task 6).

### 1.0 Implementation status and corrections (Task 7, 2026-10-09)

Implemented in `services/fleet` (README there is the reference). Where the implementation differs from the draft below, this list wins:

- **No outbox.** Tuples are written through `TupleWriter(owner="fleet")` inside the database transaction, right before the commit (Identity's pattern): OpenFGA down means rollback and **503**, no event; a failed commit after the write undoes the tuple change. Events are published after the commit. There is no `outbox` table, no `authz_sync` field and no `MOWDECK_OUTBOX_RETRY_MAX_SECONDS`. Reasons in ADR 0016.
- **Identity's events** are `identity.account.created` (`AccountCreated`) and `identity.grants.changed` (`GrantsChanged`), not `account.created` and `authz.tuples_changed`. Identity publishes no account deletion event in Phase 1, so Fleet does not purge accounts yet. Cache invalidation uses `mowdeck_authz.events.subscribe_cache_invalidation`; Fleet publishes no invalidation of its own (other services see hierarchy changes after their 5 s cache TTL).
- **No `link_status`** and no `fleet-links` consumer: connection state is shown by the Device-Gateway (`GET /api/v1/device-gateway/mowers`) and later by Telemetry.
- **Account scoping**: the API has no account in the path; the account is always the token's `acc` (Identity uses `/accounts/{account_id}/...`; Fleet keeps the paths of 1.4). A foreign owner gets 404 on every object route and empty lists.
- **`mowdeck_authz.fastapi.authorize(request, relation, obj, hide=True)`** was added to the library (no `principal` argument); the command routes call it with `relation_for(kind)` before any I/O. Fleet does not enable the token pre-filter.
- **Command record**: column and API field `status` (not `state`), plus `result_at`; `issued_by_subject` and `issued_by` (JSON `PrincipalRef`). Emergency stop has its own short expiry (`MOWDECK_EMERGENCY_STOP_TTL_SECONDS`, 10 s), manual drive `MOWDECK_MANUAL_DRIVE_TTL_SECONDS` (2 s). Retention uses `pg_try_advisory_xact_lock`, interval `MOWDECK_COMMAND_RETENTION_INTERVAL_SECONDS`.
- **Consumers**: `command.result.mower.*` with durable and deliver group `fleet-command-results`, `event.identity.account.created` with `fleet-accounts`, `fleet.mowers.list` with queue `fleet` (as designed).
- **Contracts** (`mowdeck_contracts.v1.commands`): `CommandAccepted` also carries `status: "accepted"`; `CommandView` has `status` and `result_at` instead of `state` and `updated_at`; `BulkCommandItem` is `{mower_id, outcome: accepted|forbidden|not_published, command_id?, expires_at?}` instead of `accepted` plus `reason`; `CommandRequest.ttl_seconds` allows up to 3600 and is capped by the setting. No paging fields were added to `MowerListRequest`.
- **Data model details**: `mower_groups` has a surrogate `id` plus a unique pair; site names are unique per account among non-default sites; an append-only `audit_log` records site, group and mower changes; `MowerRegistration` events go out on name, connection, site and group changes.
- **Manual driving (Task 16)**: the `POST .../commands` path with `manual_drive` stays (2 s expiry) but is not the joystick path; clients drive over the Realtime WebSocket with a session Fleet records (`fleet.drive.start`/`stop`, table `drive_sessions`, `GET /mowers/{id}/drive-sessions`) and frames that Realtime publishes directly ([ADR 0026](../adr/0026-manual-drive-channel.md)).
- **Not done in Task 7**: the e2e skeleton of section 5 (left for Task 9 with Telemetry and Realtime). The Compose stack was checked by hand (register, sub-user with Operator, register a simulated mower, gateway connected, start command executed, foreign owner 404).

### 1.1 Boundary

Fleet owns sites, groups, mowers, their placement in the hierarchy and the command record. **Shares and assignments are Identity's job**: "Anna is Operator on group North" is a role assignment in Identity's database, expanded into `grant_*` tuples by Identity (identity design 1.2, 1.4). The plan's table lists "Freigaben" under Fleet, but the approved identity design moved them to Identity; Fleet only provides the objects (`group:<id>`) that assignments point at, and Identity validates an assignment target by reading the object's `account` tuple that Fleet wrote (identity design 2.6, "Tenancy rules").

### 1.2 Data model (PostgreSQL, Alembic)

All ids are UUID v4 generated by Fleet; all timestamps `timestamptz` UTC; every table carries `account_id` and every query filters by it.

```sql
CREATE TABLE sites (
  id            uuid PRIMARY KEY,
  account_id    uuid NOT NULL,
  name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  is_default    boolean NOT NULL DEFAULT false,   -- the hidden household site
  address       jsonb,                            -- {street, postal_code, city, country}
  timezone      text NOT NULL,                    -- IANA name, validated with zoneinfo
  created_by    uuid,                             -- NULL for the default site (system)
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX sites_one_default ON sites (account_id) WHERE is_default;
CREATE UNIQUE INDEX sites_name ON sites (account_id, lower(name));

CREATE TABLE groups (
  id          uuid PRIMARY KEY,
  account_id  uuid NOT NULL,
  site_id     uuid REFERENCES sites (id) ON DELETE RESTRICT,  -- NULL: cross-site group
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  created_by  uuid NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX groups_name ON groups (account_id, lower(name));

CREATE TABLE mowers (
  id                  uuid PRIMARY KEY,
  account_id          uuid NOT NULL,
  site_id             uuid NOT NULL REFERENCES sites (id) ON DELETE RESTRICT,
  name                text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  model               text CHECK (length(model) <= 100),       -- "LUBA 2 AWD 5000", "simulator"
  serial              text CHECK (length(serial) <= 64),
  connection_kind     text NOT NULL,                           -- ConnectionKind
  connection_options  jsonb NOT NULL DEFAULT '{}'::jsonb,      -- no secrets, max 4 KiB
  link_status         text NOT NULL DEFAULT 'unknown',         -- connected|disconnected|unknown
  link_changed_at     timestamptz,
  link_reason         text,
  created_by          uuid NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX mowers_serial ON mowers (account_id, serial) WHERE serial IS NOT NULL;
CREATE INDEX mowers_account ON mowers (account_id, site_id);

CREATE TABLE mower_groups (
  mower_id    uuid NOT NULL REFERENCES mowers (id) ON DELETE CASCADE,
  group_id    uuid NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
  account_id  uuid NOT NULL,
  PRIMARY KEY (mower_id, group_id)
);

CREATE TABLE commands (                    -- command audit and latest status
  command_id      uuid PRIMARY KEY,
  account_id      uuid NOT NULL,
  mower_id        uuid NOT NULL,           -- no FK: the record outlives the mower
  bulk_id         uuid,
  kind            text NOT NULL,
  command         jsonb NOT NULL,
  priority        text NOT NULL,
  issued_by       jsonb NOT NULL,          -- PrincipalRef
  issued_at       timestamptz NOT NULL,
  expires_at      timestamptz NOT NULL,
  correlation_id  text NOT NULL,
  state           text NOT NULL,           -- pending|accepted|rejected|executed|expired|failed
  reason          text,
  updated_at      timestamptz NOT NULL
);
CREATE INDEX commands_mower ON commands (account_id, mower_id, issued_at DESC);

CREATE TABLE outbox (                      -- tuples and events, drained in id order
  id          bigserial PRIMARY KEY,
  created_at  timestamptz NOT NULL DEFAULT now(),
  kind        text NOT NULL,               -- 'tuples' | 'event'
  payload     jsonb NOT NULL,              -- {writes, deletes} or an EventEnvelope
  attempts    int NOT NULL DEFAULT 0,
  last_error  text,
  done_at     timestamptz
);
```

Notes:

- The model has one `site` tuple per mower (always) plus zero or more `group` tuples (identity design 1.1). `mowers.site_id NOT NULL` mirrors that.
- A mower's groups may belong to other sites of the same account (cross-site groups, plan "auch standortübergreifend"); a group of another account is answered 404.
- `connection_options` is validated as a JSON object of at most `MOWDECK_CONNECTION_OPTIONS_MAX_BYTES` and must not contain keys matching `(?i)pass|secret|token|key|credential` (the contract docstring forbids secrets, `fleet.py`). Kind-specific validation stays in the Device-Gateway adapter, which reports a failing link in `mower.disconnected.reason`; Fleet must not import the GPL adapter package (ADR 0011).
- `link_status` is fed by `event.mower.connected|disconnected` (durable queue consumer `fleet-links`), so the mower list shows online state without a call to Telemetry.

### 1.3 Use cases

**Default site.** `ensure_default_site(account_id)` is `INSERT ... ON CONFLICT DO NOTHING` on `sites_one_default`, followed by a select, plus the tuple `site:<id>#account@account:<acc>` through the outbox. It runs on `event.account.created` (durable `fleet-accounts`) and lazily in every use case that needs a site when none is given. The lazy path is what makes the e2e test deterministic: the owner registers and immediately creates a mower.

**Sites and groups.** Create, list, get, update (name, address, timezone; group: name and parent site), delete. Delete refuses with 409 while mowers or groups reference the object; the default site cannot be deleted or renamed by the API. Changing a group's parent site deletes `group#parent@site:old` and writes `group#parent@site:new` in one tuple transaction.

**Register a mower** (`POST /mowers`):

```mermaid
sequenceDiagram
    autonumber
    actor O as Owner
    participant FL as Fleet
    participant Z as OpenFGA
    participant DB as Fleet DB
    participant N as NATS
    participant D as Device-Gateway
    participant T as Telemetry
    O->>FL: POST /api/v1/fleet/mowers {name, connection_kind: simulated}
    FL->>Z: check can_manage_mowers on site (default site if omitted) and each group
    FL->>DB: tx: mower row, mower_groups, outbox[tuples], outbox[event mower.registered]
    FL->>Z: drain tuples: mower:M#account@account:A, mower:M#parent@site:S, #parent@group:G...
    FL->>N: drain event.mower.registered (MowerRegistration, msg id = event id)
    FL-->>O: 201 {id, ..., authz_sync: "done"}
    N-->>D: event.mower.registered
    D->>D: shard owns M? create session, connect simulator
    D->>N: event.mower.connected, telemetry.mower.M (docked)
    N-->>T: telemetry.mower.M, store latest
    N-->>FL: event.mower.connected, link_status = connected
```

Rules: quota check `MOWDECK_MAX_MOWERS_PER_ACCOUNT` (Entitlements replaces it in Phase 4); `connection_kind` must be in `MOWDECK_ALLOWED_CONNECTION_KINDS`; `created_by` is `principal.subject`. If the synchronous drain fails (OpenFGA down), the response is still 201 with `"authz_sync": "pending"` and the event is **not** published yet, because the outbox is drained in order; the background worker retries with backoff up to `MOWDECK_OUTBOX_RETRY_MAX_SECONDS`. This fails closed: nobody sees or connects the mower until its tuples exist. Same pattern as Identity's outbox (identity design 3.7).

**Update a mower.** Name, model, serial, connection kind and options, site. Publishes `mower.updated` with the full `MowerRegistration` (the Device-Gateway renames in place or reconnects when the link changed, ADR 0013). A site move rewrites `mower#parent@site`.

**Move between groups** (`PUT /mowers/{id}/groups {group_ids}`): computes added and removed groups, requires `can_manage_mowers` on the mower and on every added or removed group, writes and deletes `mower#parent@group:*` in one tuple transaction. No bus event is needed by the Device-Gateway; the `TupleWriter` publishes `authz.tuples_changed` (Task 6 contract) so Realtime re-checks subscriptions.

**Delete a mower.** Deletes the row (cascade `mower_groups`), outbox: delete `mower#account` and every `mower#parent` tuple, then `mower.removed`. Commands stay in `commands` for the audit retention.

**`event.account.deleted`** (durable `fleet-accounts`): deletes all sites, groups and mowers of the account with their tuples, publishes `mower.removed` per mower, deletes the account's command records (identity design 3.8 lists Fleet's part).

**Answer `fleet.mowers.list`.** Core NATS subscription with `queue="fleet"` so exactly one replica answers. The handler reads `(id, account_id, name, connection_kind, connection_options)` of all mowers with a server-side cursor, keeps those with `id.int % shard_count == shard_index` (the rule of `MowerListRequest`, `fleet.py`) and replies `MowerListReply`. Filtering in Python is fine for Phase 1 sizes; it is a system path without a principal and therefore without account filter. Limit: NATS's default max payload is 1 MiB **[unverified for 2.15, documented default]**, about 4 000 registrations; Section 6 adds optional paging fields for later. Risk: any NATS client can ask for all registrations; NATS accounts and subject permissions are a later hardening step (no secrets are in the payload).

**Send a command** (`POST /mowers/{id}/commands`):

```mermaid
sequenceDiagram
    autonumber
    actor A as Anna (Operator)
    participant FL as Fleet
    participant Z as OpenFGA
    participant DB as Fleet DB
    participant N as NATS
    participant D as Device-Gateway
    A->>FL: POST /mowers/M/commands {command: {kind: start_mow, area_ids: [front]}}
    FL->>FL: relation = relation_for("start_mow") = can_control
    FL->>Z: batch_check [can_control, can_view] user:anna mower:M
    alt neither
        FL-->>A: 404 (nothing written, nothing published)
    else view only
        FL-->>A: 403 (nothing written, nothing published)
    end
    FL->>DB: SELECT mower WHERE id = M AND account_id = acc (404 if missing)
    FL->>FL: CommandEnvelope.create(account_id = row.account_id, issued_by = principal.ref(), ttl)
    FL->>DB: INSERT commands (state pending)
    FL->>N: publish_command (msg id = command_id, Mowdeck-Priority header)
    FL-->>A: 202 CommandAccepted {command_id, expires_at, status_url}
    N->>D: deliver, admit, execute
    D->>N: command.result.mower.M (executed)
    N-->>FL: durable fleet-command-results, UPDATE commands SET state = executed
```

```python
# fleet/domain/commands.py (pure)
COMMAND_RELATION: Final[Mapping[str, Relation]] = {
    "start_mow": Relation.CAN_CONTROL,
    "pause_mow": Relation.CAN_CONTROL,
    "resume_mow": Relation.CAN_CONTROL,
    "return_to_dock": Relation.CAN_CONTROL,
    "set_blade_height": Relation.CAN_CONTROL,
    "set_speed": Relation.CAN_CONTROL,
    "manual_drive": Relation.CAN_DRIVE,
    "emergency_stop": Relation.CAN_EMERGENCY_STOP,  # = can_view in the model
}


def command_ttl(kind: str, requested: int | None, settings: FleetSettings) -> timedelta:
    if kind == "manual_drive":
        return timedelta(seconds=2)  # one joystick frame; the dead-man ttl_ms lives in the command
    seconds = requested or settings.command_ttl_seconds
    return timedelta(seconds=min(seconds, settings.command_ttl_max_seconds))


# fleet/adapters/api/commands.py
@router.post("/mowers/{mower_id}/commands", status_code=202)
async def send_command(
    mower_id: UUID,
    body: CommandRequest,
    request: Request,
    response: Response,
    account_id: CurrentAccountId,
    principal: CurrentPrincipal,
    service: CommandServiceDep,
) -> CommandAccepted:
    relation = COMMAND_RELATION[body.command.kind]
    await authorize_or_hide(
        request, principal, relation, mower(mower_id)
    )  # 404 / 403, no I/O after a deny
    accepted = await service.send(principal, account_id, mower_id, body)
    response.headers["Location"] = accepted.status_url
    return accepted
```

`authorize_or_hide` is the body of `require_or_not_found` (`mowdeck_authz/fastapi.py`) called with a relation chosen at runtime; a small public helper `mowdeck_authz.fastapi.authorize(request, principal, relation, obj, hide=True)` should be added to the library instead of copying it **[assumption]**. For `emergency_stop` the relation equals the visibility relation, so a foreign mower is 404 and every visible mower is allowed.

Publishing: `publish_command` refuses expired envelopes and adds `Mowdeck-Priority` (kit README). If the bus is not connected, the command row is set to `failed` with reason `not published` and the API answers 503; no retry, because a late "start" is exactly what the expiry protects against (ADR 0009).

**Command results.** Durable consumer `fleet-command-results` with `queue="fleet-command-results"` on `command.result.mower.*` (`ALL_COMMAND_RESULTS`). Update rule: `UPDATE commands SET state = :s, reason = :r, updated_at = :at WHERE command_id = :id AND rank(state) < rank(:s)` with `pending < accepted < {rejected, executed, expired, failed}`, so redeliveries and out-of-order results are harmless. Unknown command ids (for example from `send_command.py`) are ignored. This replaces the "in memory with TTL" variant: with several Fleet replicas an in-memory map would need every replica to see every result, and the plan wants command audit anyway ("jeder Steuerbefehl wird mit Wer, Was, Wann gespeichert"). Retention `MOWDECK_COMMAND_RETENTION_DAYS` (default 90), purged hourly under an advisory lock.

**Bulk command** (`POST /groups/{id}/commands`, `POST /sites/{id}/commands`). The plan: "Fleet zerlegt in Einzelbefehle" and "Fleet prüft jeden einzelnen Mäher, und das Ergebnis zeigt pro Mäher, ob der Befehl angekommen ist".

1. Target gate: `require_or_not_found(can_bulk_command, group|site)`; for `emergency_stop` the gate is `can_view` on the target (see open question 2).
2. Load the target's mowers with the account filter (max `MOWDECK_BULK_MAX_MOWERS`, else 422).
3. One `check_many` with `[relation, can_view]` per mower.
4. Allowed: own `CommandEnvelope` and `commands` row with a shared `bulk_id`, published one by one. Visible but not allowed: item `accepted: false, reason: "forbidden"`. Not visible: omitted, so a group never reveals mowers the caller cannot see.
5. 202 `BulkCommandAccepted` with one item per visible mower. Results arrive per command as usual.

### 1.4 REST API, prefix `/api/v1/fleet`

Routers are mounted under `/api/v1` by the kit and carry the `/fleet` prefix themselves; Traefik does not strip (same as `device-gateway`, whose routes are `/api/v1/device-gateway/...`). "hide" means `require_or_not_found` (404 when not visible), "403" means `require`.

| Method and path | Required relation | Notes |
| --- | --- | --- |
| `GET /sites` | `can_view` per site | `list_objects(can_view, site)`, short-circuit when `can_view` on `account:<acc>`; default site hidden unless `?include_default=true` |
| `POST /sites` | `can_manage_mowers` on account (403) | |
| `GET /sites/{site_id}` | `can_view` on site (hide) | |
| `PATCH`, `DELETE /sites/{site_id}` | `can_manage_mowers` on site (hide) | 409 if referenced; default site immutable |
| `GET /groups` | `can_view` per group | same list pattern; `?site_id=` |
| `POST /groups` | `can_manage_mowers` on parent site, or on account for a cross-site group | |
| `GET /groups/{group_id}` | `can_view` on group (hide) | includes mower ids visible to the caller |
| `PATCH`, `DELETE /groups/{group_id}` | `can_manage_mowers` on group (hide); a parent change also needs it on the new site | |
| `GET /mowers` | `can_view` per mower | `?site_id=&group_id=`; short-circuit on account-level view; otherwise `list_objects` then `WHERE id = ANY(:ids) AND account_id = :acc` |
| `POST /mowers` | `can_manage_mowers` on the target site and on each group | 201, `Location` |
| `GET /mowers/{mower_id}` | `can_view` (hide) | |
| `PATCH /mowers/{mower_id}` | `can_manage_mowers` (hide), plus on the new site when moving | |
| `PUT /mowers/{mower_id}/groups` | `can_manage_mowers` on mower (hide) and on each added or removed group | |
| `DELETE /mowers/{mower_id}` | `can_manage_mowers` (hide) | 204 |
| `POST /mowers/{mower_id}/commands` | by kind: `can_control`, `can_drive`, `can_emergency_stop` (hide) | 202 `CommandAccepted` |
| `GET /mowers/{mower_id}/commands` | `can_view` (hide) | keyset pagination on `issued_at` |
| `GET /commands/{command_id}` | row by id and account, then `can_view` on its mower | 404 if either fails |
| `POST /groups/{group_id}/commands` | `can_bulk_command` on group (hide), per mower as above | 202 `BulkCommandAccepted` |
| `POST /sites/{site_id}/commands` | `can_bulk_command` on site (hide), per mower as above | 202 |

`ModuleDescriptor`: permissions `mower.view`, `mower.control`, `mower.drive`, `fleet.manage`, `fleet.bulk_command` (descriptions only; Identity collects them, identity design 3.2); events `mower.registered`, `mower.updated`, `mower.removed`; UI area `/mowers` with `mower.view`.

### 1.5 Bus interfaces

| Direction | Subject | Kind | Payload |
| --- | --- | --- | --- |
| produced | `event.mower.registered|updated|removed` | `EVENTS` | `EventEnvelope[MowerRegistration]`, `producer="fleet"`, `account_id` set |
| produced | `command.mower.<id>` | `COMMANDS` | `CommandEnvelope` |
| reply | `fleet.mowers.list` | core NATS, queue `fleet` | `MowerListRequest` / `MowerListReply` |
| consumed | `command.result.mower.*` | durable + queue `fleet-command-results` | `CommandResult` |
| consumed | `event.mower.connected|disconnected` | durable + queue `fleet-links` | `MowerConnectionEvent` |
| consumed | `event.account.created|deleted` | durable + queue `fleet-accounts` | Task 6 payloads |

Durable plus `queue` makes replicas share one consumer (kit `bus/base.py`: "Subscribers sharing a queue (or durable) name form a group"). The kit creates consumers with `DeliverPolicy.NEW` (`bus/jetstream.py`), so a brand-new durable starts at the end of the stream; for `fleet-accounts` this is harmless because the default site is also created lazily.

### 1.6 Settings (besides the kit's and `MOWDECK_OPENFGA_*`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOWDECK_SERVICE_NAME` | `fleet` | |
| `MOWDECK_DATABASE_URL(_FILE)` | required | `postgresql://fleet:...@postgres:5432/fleet` |
| `MOWDECK_COMMAND_TTL_SECONDS` | `30` | default expiry of a command |
| `MOWDECK_COMMAND_TTL_MAX_SECONDS` | `300` | upper bound for `ttl_seconds` in the request |
| `MOWDECK_COMMAND_RETENTION_DAYS` | `90` | command audit retention |
| `MOWDECK_MAX_MOWERS_PER_ACCOUNT` | `50` | until Entitlements exists |
| `MOWDECK_ALLOWED_CONNECTION_KINDS` | `simulated` | comma list; Phase 2 adds `ble_bridge,cloud` |
| `MOWDECK_CONNECTION_OPTIONS_MAX_BYTES` | `4096` | |
| `MOWDECK_BULK_MAX_MOWERS` | `200` | |
| `MOWDECK_DEFAULT_TIMEZONE` | `Europe/Berlin` | timezone of the hidden default site **[assumption]** |
| `MOWDECK_OUTBOX_RETRY_MAX_SECONDS` | `60` | backoff cap of the outbox worker |
| JWT verification | from `mowdeck-auth` | names defined by Task 6 (issuer, audience, JWKS URL) |

### 1.7 Tests

Unit (domain): relation per command kind for all eight kinds; TTL rules (manual drive fixed, cap applied); state rank update rule; shard filter equals `mower_id.int % n`; connection option validation (size, secret-looking keys).

API with `make_test_app`, `InMemoryBus`, `InMemoryAuthorizer` (writes through a real `TupleWriter(owner="fleet")` into the fake), PostgreSQL via `MOWDECK_TEST_DATABASE_URL`:

- Register as owner: row, the three expected tuples (`account`, `parent@site`, `parent@group`), one `mower.registered` on the bus with the right `MowerRegistration`; tuples are written before the event (outbox order).
- Default site created lazily when the account event has not arrived; a later `account.created` creates no second site.
- Operator at account level can command, cannot create a mower (403 on account-level create).
- **Cross-account negatives**, one parametrised test over the route table: owner of account B gets 404 on every `/mowers/{A}`, `/sites/{A}`, `/groups/{A}`, `/commands/{A's command}` route and every method; `GET /mowers` never contains A's ids; smuggling `account_id` in query or body changes nothing; registering a mower into A's site or group is 404.
- **A refused command never reaches the bus**: for Viewer `start_mow` (403), for B's owner any command (404), assert `bus.published == []` for `command.mower.*` and no `commands` row.
- Viewer `emergency_stop`: 202, envelope priority `emergency`.
- `issued_by` equals `principal.ref()` and `account_id` equals the mower's account, never a body value.
- Expired publish refused, bus down gives 503 and state `failed`.
- Results: executed after accepted updates; accepted after executed is ignored; unknown id ignored; cross-account `GET /commands/{id}` is 404.
- Bulk: mixed visibility (allowed, view only, invisible) yields accepted, forbidden and omitted items; one envelope per allowed mower; emergency stop bulk with view only.
- `fleet.mowers.list`: two shards return disjoint sets whose union is all mowers; only one replica answers (two subscribers in one queue group on the in-memory bus).
- Group move writes and deletes the right parent tuples and nothing else; `TupleWriter` ownership error if a Fleet code path tried a `grant_*` (guard test).
- Outbox: OpenFGA failure leaves `authz_sync: pending`, no event published; worker drains later in order.
- JetStream test with `MOWDECK_TEST_NATS_URL`: command published by Fleet is received by a durable consumer, result updates the row.

---

## 2. Telemetry service (Task 8)

### 2.0 Implementation status and corrections (Task 8, 2026-10-09)

Implemented in `services/telemetry` (README there is the reference, [ADR 0017](../adr/0017-telemetry-storage-and-retention.md) records the decisions). Where the implementation differs from the draft below, this list wins:

- **Consumers**: `telemetry.mower.*` with durable and queue group `telemetry-states`, `command.result.mower.*` with `telemetry-results`, `event.mower.*` (registered, updated, removed) with `telemetry-mowers`. No `event.account.deleted` consumer: Identity publishes no account deletion event in Phase 1, so accounts are not purged yet.
- **Tables**: `mower_directory` (not `mowers`), `mower_latest`, `mower_history`, `mower_positions`, `command_timeline` (not `command_results`). The latest row keeps the history time and the fingerprints of the last history and position write (`history_written_at`, `history_fingerprint`, `position_fingerprint`). The history has no position columns: the position of a bin is joined from the track. Indexes on `(mower_id, observed_at desc)` and `(account_id, observed_at desc)` plus BRIN on the time column.
- **Write rules**: history is appended when the significant fields change or the last history row is `MOWDECK_TELEMETRY_HEARTBEAT_HISTORY_SECONDS` (default 300, not 60) old; a position is appended when it (or the RTK fix) differs from the last stored one, there is no distance threshold. A state of another account than the directory or the stored row is dropped with a warning.
- **Directory**: events replace entries, the `fleet.mowers.list` seed only adds missing ones; `mower.removed` purges the mower's rows in all tables. A result for an unknown mower is redelivered up to `MOWDECK_TELEMETRY_UNKNOWN_MOWER_ATTEMPTS` (3) times, then dropped.
- **API**: `GET /mowers/{id}/state` answers 404 with the problem type `no-telemetry-yet` for a visible mower without a state; the batch route `GET /mowers/state?mower_id=...` became `GET /accounts/me/mowers/states` (all viewable mowers of the account, `list_objects`, bounded); history takes `step` (`5s`, `1m`, `1h`, `1d`) instead of `bucket` and returns battery mean, min and max, charging, RTK fix, samples and position per bin; the command timeline takes `limit` only (no cursor).
- **Settings** carry the prefix `MOWDECK_TELEMETRY_` (`..._HEARTBEAT_HISTORY_SECONDS`, `..._RETENTION_HISTORY_DAYS` 30, `..._RETENTION_POSITIONS_DAYS` 90, `..._RETENTION_COMMANDS_DAYS` 30, `..._RETENTION_INTERVAL_SECONDS`, `..._QUERY_MAX_RANGE_DAYS`, `..._QUERY_MAX_ROWS`, `..._STATES_LIMIT`, `..._STALE_AFTER_SECONDS`).
- **Retention** takes a session level `pg_try_advisory_lock` on a dedicated connection and deletes in batches of 5 000 rows; the in-memory store has no lock.
- **Permission**: `telemetry.view` is declared with `implied_by=["mower.view"]` for the catalogue; the routes check `can_view` on the mower (`require_or_not_found`), not the token's `perms`.
- **Not done**: the e2e step "telemetry latest shows docked" belongs to the acceptance test (Task 9); the Compose stack was verified by hand (register, simulated mower, state `docked`, `start_mow`, `mowing`, history bins, command timeline, foreign owner 404).

Generated with `needs_db=true`, package `telemetry`, database and role `telemetry` (exist). It depends only on contracts, the kit, `mowdeck-authz` and `mowdeck-auth`, so it can be built in parallel with Fleet.

### 2.1 Ingestion

| Subject | Consumer | Purpose |
| --- | --- | --- |
| `telemetry.mower.*` (`ALL_TELEMETRY`) | durable + queue `telemetry-state` | latest, history, positions |
| `command.result.mower.*` | durable + queue `telemetry-command-results` | command result timeline |
| `event.mower.registered|updated|removed` | durable + queue `telemetry-mowers` | mower directory (`mower_id -> account_id`), purge on removal |
| `event.account.deleted` | durable + queue `telemetry-accounts` | purge the account |

The brief writes `telemetry.mower.>`; the contracts define `ALL_TELEMETRY = "telemetry.mower.*"` (`subjects.py`), which is equivalent for per-mower subjects and is used here.

**Tenant attribution.** `MowerState` carries `account_id`, set by the Device-Gateway from the Fleet registration, so state rows take it from the message. `CommandResult` has no account id, so Telemetry keeps a small `mowers` directory filled from Fleet's events and, on start, from one `fleet.mowers.list` request with `shard_index=0, shard_count=1` (the kit's durables start at new messages, so a Telemetry deployed after Fleet would otherwise miss earlier registrations). A state whose `account_id` differs from the directory entry is dropped with a warning (defence in depth, mirrors the gateway's admission rule). A result for an unknown mower is dropped.

**Write rules per state message:**

1. Upsert `mower_state_latest` only if `observed_at >= current.observed_at` (out-of-order guard; redeliveries are no-ops).
2. Append to `mower_state_history` if the fingerprint (activity, battery percent, charging, RTK fix and source, connection, blade height, speed, error code) changed, or the last stored row is older than `MOWDECK_HISTORY_MIN_INTERVAL_SECONDS`. The gateway republishes an unchanged state every 10 s as a heartbeat (ADR 0013); storing every heartbeat would multiply the table for no information. `ON CONFLICT (mower_id, observed_at) DO NOTHING` makes redelivery idempotent.
3. Append to `mower_positions` when a position is present and it moved at least `MOWDECK_POSITION_MIN_DISTANCE_M` or the RTK fix changed. This is the raw material for the Phase 3 RTK signal map ("Positionen mit Fix-Qualität und Korrekturquelle loggen").

Replicas in one queue group can process two messages of the same mower concurrently; rule 1 keeps "latest" correct, rule 2 may store one extra row, which is acceptable.

### 2.2 Data model

```sql
CREATE TABLE mowers (                       -- directory from Fleet events
  mower_id uuid PRIMARY KEY, account_id uuid NOT NULL, updated_at timestamptz NOT NULL
);

CREATE TABLE mower_state_latest (
  mower_id            uuid PRIMARY KEY,
  account_id          uuid NOT NULL,
  observed_at         timestamptz NOT NULL,
  received_at         timestamptz NOT NULL,
  activity            text NOT NULL,
  battery_percent     smallint NOT NULL,
  state               jsonb NOT NULL,        -- the full MowerState, served as is
  history_written_at  timestamptz,
  history_fingerprint text
);
CREATE INDEX mower_state_latest_account ON mower_state_latest (account_id);

CREATE TABLE mower_state_history (
  account_id       uuid NOT NULL,
  mower_id         uuid NOT NULL,
  observed_at      timestamptz NOT NULL,
  activity         text NOT NULL,
  battery_percent  smallint NOT NULL,
  charging         boolean NOT NULL,
  rtk_fix          text NOT NULL,
  connection       text NOT NULL,
  blade_height_mm  smallint,
  speed_mps        real,
  error_code       text,
  PRIMARY KEY (mower_id, observed_at)
);
CREATE INDEX mower_state_history_time ON mower_state_history USING brin (observed_at);

CREATE TABLE mower_positions (
  account_id uuid NOT NULL, mower_id uuid NOT NULL, observed_at timestamptz NOT NULL,
  lat double precision NOT NULL, lon double precision NOT NULL, heading real,
  rtk_fix text NOT NULL, rtk_source text NOT NULL,
  PRIMARY KEY (mower_id, observed_at)
);

CREATE TABLE command_results (
  account_id uuid NOT NULL, mower_id uuid NOT NULL, command_id uuid NOT NULL,
  status text NOT NULL, reason text, at timestamptz NOT NULL,
  PRIMARY KEY (command_id, status)          -- one row per transition; msg id is <command_id>:<status>
);
CREATE INDEX command_results_mower ON command_results (mower_id, at DESC);
```

History is not stored as `jsonb` on purpose: the columns are what reports and downsampling need, and `raw` is adapter specific ("Consumers must not rely on its structure", `mower.py`). TimescaleDB later: `mower_state_history` and `mower_positions` become hypertables on `observed_at` with the same primary keys and columns, and `date_bin` turns into `time_bucket`; nothing in the API changes. Plain PostgreSQL 17 (the Compose image) is enough for Phase 1.

### 2.3 REST API, prefix `/api/v1/telemetry`

Every endpoint takes the mower from the path, runs `require_or_not_found(can_view, path_object("mower", "mower_id"))` and queries `WHERE account_id = :principal_account AND mower_id = :id` (the three isolation layers of ADR 0014).

| Method and path | Result |
| --- | --- |
| `GET /mowers/{mower_id}/state` | `MowerStateSnapshot` (state, `received_at`, `stale` if older than `MOWDECK_STALE_AFTER_SECONDS`); 404 when nothing was received yet |
| `GET /mowers/state?mower_id=...&mower_id=...` | latest of up to 200 mowers; `check_many(can_view)` drops invisible ids silently (list view) |
| `GET /mowers/{mower_id}/history?from=&to=&bucket=` | `StateHistory`; `bucket` in `raw, 1m, 5m, 15m, 1h, 1d`; range at most `MOWDECK_QUERY_MAX_RANGE_DAYS`; at most `MOWDECK_QUERY_MAX_POINTS` points, `truncated` flag |
| `GET /mowers/{mower_id}/positions?from=&to=&limit=` | `PositionTrack` |
| `GET /mowers/{mower_id}/commands?from=&to=&limit=&cursor=` | `CommandLog`, newest first, keyset on `(at, command_id)` |

Downsampling (PostgreSQL 14 or newer has `date_bin`):

```sql
SELECT date_bin(:bucket, observed_at, TIMESTAMPTZ '2000-01-01') AS at,
       (array_agg(activity ORDER BY observed_at DESC))[1]       AS activity,
       round(avg(battery_percent))::int                          AS battery_percent,
       min(battery_percent) AS battery_min, max(battery_percent) AS battery_max,
       bool_or(charging) AS charging,
       (array_agg(rtk_fix ORDER BY observed_at DESC))[1]        AS rtk_fix,
       (array_agg(error_code ORDER BY observed_at DESC) FILTER (WHERE error_code IS NOT NULL))[1] AS error_code,
       count(*) AS samples
FROM mower_state_history
WHERE account_id = :account_id AND mower_id = :mower_id
  AND observed_at >= :from AND observed_at < :to
GROUP BY 1 ORDER BY 1
LIMIT :max_points + 1;
```

### 2.4 Retention job

A background task started in `on_startup`, every `MOWDECK_RETENTION_INTERVAL_SECONDS`. It takes `pg_try_advisory_lock(<constant>)` so only one replica works, then deletes in batches of 5 000 rows (`DELETE ... WHERE ctid = ANY(ARRAY(SELECT ctid ... LIMIT 5000))`) until nothing is left, per table: history after `MOWDECK_HISTORY_RETENTION_DAYS`, positions after `MOWDECK_POSITION_RETENTION_DAYS`, command results after `MOWDECK_COMMAND_LOG_RETENTION_DAYS`. `mower.removed` and `account.deleted` purge immediately. Metrics: rows deleted per table, job duration.

### 2.5 Settings

| Variable | Default |
| --- | --- |
| `MOWDECK_SERVICE_NAME` | `telemetry` |
| `MOWDECK_DATABASE_URL(_FILE)` | required |
| `MOWDECK_HISTORY_MIN_INTERVAL_SECONDS` | `60` |
| `MOWDECK_POSITION_MIN_DISTANCE_M` | `0.25` |
| `MOWDECK_HISTORY_RETENTION_DAYS` | `30` |
| `MOWDECK_POSITION_RETENTION_DAYS` | `90` |
| `MOWDECK_COMMAND_LOG_RETENTION_DAYS` | `30` |
| `MOWDECK_RETENTION_INTERVAL_SECONDS` | `3600` |
| `MOWDECK_STALE_AFTER_SECONDS` | `60` (six missed heartbeats) |
| `MOWDECK_QUERY_MAX_RANGE_DAYS` | `31` |
| `MOWDECK_QUERY_MAX_POINTS` | `5000` |

### 2.6 Tests

Ingestion with `InMemoryBus` and the test database: latest upsert ignores an older `observed_at`; redelivery inserts nothing; unchanged heartbeats within the interval store one history row, a battery change stores one more; positions skip sub-threshold moves; a state whose account differs from the directory is dropped; command results stored per transition and attributed via the directory; directory seeded from a stub `fleet.mowers.list` responder; `mower.removed` purges all four tables for that mower only.

API: `require_or_not_found` 200, 403 (not possible for `can_view` alone, so only 200 and 404), 404 for a foreign mower on every route (parametrised); a query parameter `account_id` is ignored; batch latest drops foreign ids without error; history bucket arithmetic on fixed data (including `truncated`); range and bucket validation (422 problems). Retention: rows older than the setting are gone, newer stay, a second replica without the lock does nothing.

---

## 3. Realtime service (Task 9)

### 3.0 Implementation status and corrections (Task 9, 2026-10-09)

Implemented in `services/realtime` (README there is the reference, [ADR 0018](../adr/0018-realtime-websocket-protocol.md) records the decisions) and `tests/e2e`. Where the implementation differs from the draft in sections 3 to 5, this list wins:

- **Token to principal**: `mowdeck-auth` already had `JwtVerifier.verify(token)`; Task 9 added `JwtVerifier.verify_token(token)` returning `VerifiedToken(principal, expires_at)`, because the socket closes at `exp`. The `[assumption]` "Task 6 must expose a function" is resolved by this.
- **Close codes**: the auth timeout closes with **4401** (not 4408), like every other authentication failure; shutdown closes with **1001** (not 1012). Uvicorn itself closes WebSockets with 1012 before the lifespan shutdown, so Realtime serves with a `uvicorn.Server` subclass (`RealtimeServer`) through the new kit option `run(server_class=...)` that closes every session with 1001 first. 1011 is used for an unexpected server error. The codes are `RealtimeCloseCode` in the contracts.
- **Protocol** (`mowdeck_contracts.v1.realtime`, JSON Schemas `RealtimeClientMessage` and `RealtimeServerMessage`): `subscribed` carries `denied` (ids failing `can_view`, unknown or foreign, indistinguishable) instead of a separate `error not_found`; revocations are a `revoked {mower_ids, reason: access_revoked}` message instead of `unsubscribed` with a reason; `unsubscribed` only answers `unsubscribe`. `ready` carries `connection_id`, a `principal` summary (subject, account, kind, display name), `token_expires_at` and `server_time`. `subscribe` has no `all_visible` option (clients pass the ids from Fleet's list). Error codes: `invalid_message`, `unauthorized`, `limit_exceeded`, `internal`. Message names are `AuthMessage`, `SubscribeMessage`, ... (not `RtAuth`).
- **Re-check trigger**: `identity.grants.changed` (`GrantsChanged`), not `authz.tuples_changed`. Realtime's own handler drops the cached decisions of the named subjects and then re-checks their sockets (in this order), instead of a separate `subscribe_cache_invalidation`. `event.mower.removed` and an account deletion event are not consumed: a removed mower loses its tuples and is revoked by the next re-check (at most 60 s); Identity publishes no deletion event in Phase 1. While OpenFGA's `ping()` fails the re-checks are skipped, so an outage does not revoke every subscription.
- **Settings**: `MOWDECK_REALTIME_MAX_CONNECTIONS_PER_ACCOUNT` (50, not 100), `MOWDECK_REALTIME_MAX_CONNECTIONS_PER_USER` (10, not `..._PER_SUBJECT`), `MOWDECK_REALTIME_MAX_SUBSCRIPTIONS` (200, not `..._PER_CONNECTION` 500), `MOWDECK_REALTIME_QUEUE_SIZE` (not `..._SEND_QUEUE_SIZE`), new `MOWDECK_REALTIME_PING_SECONDS` (20: uvicorn's `ws_ping_interval` and `ws_ping_timeout`, passed through the new `**uvicorn_options` of the kit's `run()`); `MOWDECK_REALTIME_MAX_MESSAGE_BYTES` also sets uvicorn's `ws_max_size`. Exceeding a limit sends `error limit_exceeded` and closes with 4429.
- **Readiness** is the bus plus the telemetry and result subscriptions; OpenFGA is deliberately not part of it (`install_authz(readiness=False)`), so open sockets keep their updates during an OpenFGA outage.
- **Fan-out**: as designed (ephemeral JetStream push consumers with deliver policy new, no queue group, encode once, state slots plus a bounded queue, the writer empties the queue before the states). The kit option `core=True` for stream subjects was not added (`[assumption]` stays open).
- **Verified `[unverified]` items**: uvicorn 0.54 defaults `ws_ping_interval` and `ws_ping_timeout` to 20.0 (checked in its `Config`); the auto WebSocket implementation is the websockets sans-I/O protocol, which sends the pings. Traefik v3.7.14 with its default entry point timeouts keeps an upgraded socket open for 90 s without application traffic while the protocol pings run (acceptance test, step 9); no Traefik option was needed. Not verified: `forwarded_allow_ips` behind Traefik (Realtime does not use the client address) and whether Traefik skips unhealthy containers (it depends on a healthy Realtime anyway).
- **Compose**: Traefik keeps its `depends_on` and additionally waits for a healthy `realtime` (section 4.1 proposed removing it; the brief for Task 9 kept it). `MOWDECK_AUTH_*`: only `MOWDECK_AUTH_JWKS_URL` (`http://identity:8000/.well-known/jwks.json`) is set; issuer and audience use the defaults shared with Identity. Identity's registration variable is `IDENTITY_REGISTRATION` (default `open`), so the e2e job needs no extra `.env` lines; `MOWDECK_SIM_TIME_SCALE=30` is already in `.env.example`. `realtime` is in Identity's `MOWDECK_IDENTITY_MODULE_URLS` (`realtime.view`, implied by `mower.view`).
- **Tests**: the protocol is tested with uvicorn in-process (the production server class) and the `websockets` client instead of Starlette's `TestClient`; backpressure on a fake slow socket. The NATS test runs two instances on one server.
- **Acceptance test** (section 5): actors `lukas-<run>`, `anna-<run>` (Operator), `bob-<run>` (other account) and `vera-<run>` (Viewer); the e2e marker `e2e` is excluded by `addopts` (`-m 'not e2e'`) and `poe e2e` runs `pytest tests/e2e -m e2e`; the foreign owner checks `GET /fleet/mowers/{id}`, `POST .../commands`, Telemetry's state, the listing and the WebSocket (`denied`, then 12 s without an update); a bad token and a missing `auth` close with 4401; the idle check disables the client's own pings, waits 90 s, then subscribes and expects a state. Timeouts: ready 30 s, live 30 s, dock 60 s. The CI job builds the service images with the GitHub Actions cache of the docker job first and pins `actions/upload-artifact@v7.0.2`.

Generated with `needs_db=false`, package `realtime`. No database ("Realtime ... eigene Daten: keine", plan). Libraries: kit, contracts, `mowdeck-authz` (checks only, no writer), `mowdeck-auth` (token verification).

### 3.1 Endpoint and authentication

`GET /api/v1/realtime/ws` upgrades to a WebSocket. Browsers cannot set an `Authorization` header on a WebSocket. Options:

| Option | Verdict |
| --- | --- |
| Query parameter `?access_token=` | Rejected: ends up in Traefik access logs, proxy logs and browser history; a 10-minute token in a log is still a credential. |
| `Sec-WebSocket-Protocol: bearer, <jwt>` | Works in browsers, but abuses the subprotocol header, the server must echo a protocol back, header logging has the same leak, and size limits on headers apply. |
| **First message `{"type": "auth", "token": "..."}` (chosen)** | Token only travels inside the TLS-protected frame stream, never in a URL or header; the same message refreshes the token on a long-lived socket. Already anticipated by the identity design 3.5 ("token passed in the first message rather than the URL"). Cost: the server accepts the upgrade before knowing the client, mitigated by a 5 s auth timeout and per-IP connection limits in Traefik later. |

Flow: accept; wait at most `MOWDECK_REALTIME_AUTH_TIMEOUT_SECONDS` for `auth`; verify with `mowdeck-auth`'s `TokenVerifier` (same algorithm allowlist, issuer, audience, JWKS cache as HTTP); build the `Principal`; reject `SERVICE` principals and principals without `account_id`; send `ready` with the token's `exp`. Task 6 must expose a function that turns a raw token into a `Principal` (the kit's `PrincipalResolver.resolve(request)` takes an HTTP request) **[assumption, request to Task 6]**. A later `auth` message with a new token replaces the principal if `sub` and `acc` are unchanged (else close 4401) and triggers a re-check. At `exp` without refresh the socket closes with 4401 `token expired`.

`Origin` is checked against `MOWDECK_REALTIME_ALLOWED_ORIGINS` when set. Because authentication is a token in a message and not a cookie, cross-site WebSocket hijacking cannot reuse a session, but the check keeps foreign pages from opening sockets at all.

### 3.2 Protocol

Client to server: `auth`, `subscribe`, `unsubscribe`, `ping`. Server to client: `ready`, `subscribed`, `unsubscribed`, `state`, `command_result`, `error`, `pong`. All JSON text frames, one message per frame, at most `MOWDECK_REALTIME_MAX_MESSAGE_BYTES`. Models in Section 6.

```json
{"type": "subscribe", "request_id": "r1", "mower_ids": ["9a5c..."]}
{"type": "subscribed", "request_id": "r1", "mower_ids": ["9a5c..."]}
{"type": "state", "mower_id": "9a5c...", "state": {"activity": "docked", "battery_percent": 100, "...": "..."}}
{"type": "command_result", "mower_id": "9a5c...", "result": {"command_id": "...", "status": "executed"}}
{"type": "error", "request_id": "r2", "code": "not_found", "message": "mower not found", "mower_ids": ["7e01..."]}
```

`subscribe` with `"all_visible": true` subscribes to every mower the principal can view, resolved with `list_objects(can_view, mower)`. Realtime has no database, so the account-level short-circuit that Fleet uses is not available; OpenFGA caps the result at 1 000 objects (identity design 2.2), which is enough for Phase 1, and the web app may pass explicit ids from Fleet's list instead. Requested ids that fail the check are reported in one `error` with code `not_found`, whether they do not exist or belong to another account. Accepted ids are confirmed in `subscribed` and immediately followed by the last known `state` from the instance's last-value cache, if any.

Close codes: `4400` protocol error, `4401` unauthenticated or token expired, `4408` auth timeout, `4429` connection limit, `4430` slow consumer, `1012` service restart (sent to every client on shutdown so they reconnect to another instance), `1009` message too big.

### 3.3 Authorization of subscriptions

- At subscribe: `check_many([CheckItem(subject, can_view, mower(id)) ...])`.
- On `event.authz.tuples_changed` (contract owned by Task 6, identity design 2.2 and 5): for connections whose subject is in the payload, or all connections for a hierarchy change (Fleet moving a mower), re-run `check_many` for their subscriptions after the library purged its cache, drop denied ids and send `unsubscribed` with reason `access_revoked`. The brief calls this event `grants.changed`; the identity design's name `authz.tuples_changed` is used here.
- Every `MOWDECK_REALTIME_RECHECK_SECONDS` (default 60) for all connections, as a safety net for a lost event; worst-case revocation latency is therefore one recheck interval, and one cache TTL (5 s) when the event arrives.
- On `event.mower.removed`: drop the mower everywhere (`unsubscribed`, reason `mower_removed`). On `event.account.deleted`: close all sockets of that account with 4401.
- Before sending, a third check without I/O: `state.account_id == connection.account_id`. Realtime has no database, so this replaces the DB filter of the other services.

### 3.4 Fan-out

```mermaid
flowchart LR
    subgraph NATS
      T["telemetry.mower.*"]
      R["command.result.mower.*"]
      E["event.authz.tuples_changed, event.mower.removed"]
    end
    subgraph RT["Realtime instance (one of n)"]
      S1["subscription: telemetry (ephemeral, no queue)"]
      S2["subscription: results (ephemeral, no queue)"]
      H["Hub: mower_id -> set of connections, last-value cache"]
      C1["connection 1: pending states (conflated) + queue"]
      C2["connection 2: ..."]
    end
    T --> S1 --> H
    R --> S2 --> H
    E --> H
    H --> C1 --> W1["WebSocket client"]
    H --> C2 --> W2["WebSocket client"]
```

Every instance subscribes without `durable` and without `queue`, so NATS gives every instance every message, and any instance can serve any client: no sticky sessions, no shared state, horizontal scaling by adding replicas behind Traefik. The kit routes stream subjects through an ephemeral JetStream push consumer with `DeliverPolicy.NEW` (`bus/jetstream.py`); a core NATS subscription would be cheaper (no acks) and would also see JetStream-published messages, so a kit option `core=True` for stream subjects is a later optimisation **[assumption]**. Cost: every instance decodes the whole telemetry stream, fine for Phase 1. The scale-out path is one core subscription per mower that has at least one local subscriber.

The bus handler must never wait on a client. nats-py runs one callback at a time per subscription, so a blocking send would stall the fan-out for everybody:

```python
class Hub:
    def __init__(self, settings: RealtimeSettings) -> None:
        self._by_mower: dict[UUID, set[Connection]] = defaultdict(set)
        self._last_state: LRU[UUID, str] = LRU(settings.last_state_cache_size)

    async def on_telemetry(self, message: BusMessage) -> None:
        state = message.model(MowerState)  # invalid: DropMessage
        frame = RtState(mower_id=state.mower_id, state=state).model_dump_json()  # encode once
        self._last_state[state.mower_id] = frame
        for conn in self._by_mower.get(state.mower_id, ()):
            if conn.account_id == state.account_id:
                conn.offer_state(state.mower_id, frame)  # never awaits

    async def on_result(self, message: BusMessage) -> None:
        result = message.model(CommandResult)
        frame = RtCommandResult(mower_id=result.mower_id, result=result).model_dump_json()
        for conn in self._by_mower.get(result.mower_id, ()):
            conn.offer(frame)  # bounded queue; overflow marks the connection slow


class Connection:
    """One socket. A writer task drains pending states first-in-mower order, then the queue."""

    def offer_state(self, mower_id: UUID, frame: str) -> None:
        self._pending_states[mower_id] = frame  # conflation: only the newest state per mower
        self._wakeup.set()

    def offer(self, frame: str) -> None:
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.close_later(4430, "slow consumer")
        self._wakeup.set()
```

**Backpressure and slow consumers.** `state` messages are snapshots, so only the newest per mower matters: they are conflated in a dict and a slow client simply sees fewer intermediate states. `command_result`, `subscribed`, `error` and the rest go through a bounded queue of `MOWDECK_REALTIME_SEND_QUEUE_SIZE`; overflow, or a single `send` that takes longer than `MOWDECK_REALTIME_SEND_TIMEOUT_SECONDS`, closes the socket with 4430. The client reconnects and re-subscribes; the last-value cache gives it the current state at once.

**Heartbeat.** The kit serves with uvicorn (`app.py`, `run`), whose defaults send a protocol ping every 20 s and close after 20 s without pong (`ws_ping_interval`, `ws_ping_timeout`) **[unverified: uvicorn 0.54 defaults, documented as 20.0]**. Browsers answer protocol pings automatically but JavaScript cannot see them, so clients may send `{"type": "ping"}` every 25 s and expect `pong` to detect half-open links on their side.

**Limits** (per instance; a global limit would need shared state, for example NATS KV, later): `MOWDECK_REALTIME_MAX_CONNECTIONS_PER_ACCOUNT` (100), `MOWDECK_REALTIME_MAX_CONNECTIONS_PER_SUBJECT` (10), `MOWDECK_REALTIME_MAX_SUBSCRIPTIONS_PER_CONNECTION` (500). Exceeding one closes with 4429 or answers `error` `limit_exceeded`.

**Shutdown.** `on_shutdown`: stop accepting, close every socket with 1012, unsubscribe. Readiness turns 503 first (kit), so Traefik stops routing new upgrades.

### 3.5 Traefik

WebSockets pass through Traefik without special configuration: the router forwards the `Upgrade` and `Connection` headers and switches to a tunnel. Router: `PathPrefix(/api/v1/realtime)`, no strip prefix, port 8000. Notes:

- Headers: Traefik adds `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`; `Origin` passes unchanged. The kit runs uvicorn with `proxy_headers=True` but without `forwarded_allow_ips`, whose default trusts only 127.0.0.1 **[unverified]**, so behind Traefik the client IP and scheme are Traefik's. Proposed kit setting `MOWDECK_FORWARDED_ALLOW_IPS` (Compose: the `mowdeck` network range) for all services.
- Timeouts: Traefik v3 entry points have responding timeouts (`readTimeout`, `writeTimeout`, `idleTimeout`); whether they cut an upgraded connection that is idle is **[unverified]**. The 20 s protocol pings keep traffic flowing well below the documented 60 s and 180 s defaults, so no change is planned; the e2e suite holds one socket idle for 90 s through Traefik to prove it (`slow` marker).
- TLS later: `wss://` on the `websecure` entry point, nothing changes in the service.

### 3.6 Settings

| Variable | Default |
| --- | --- |
| `MOWDECK_SERVICE_NAME` | `realtime` |
| `MOWDECK_REALTIME_AUTH_TIMEOUT_SECONDS` | `5` |
| `MOWDECK_REALTIME_ALLOWED_ORIGINS` | empty (no check) |
| `MOWDECK_REALTIME_MAX_MESSAGE_BYTES` | `16384` |
| `MOWDECK_REALTIME_MAX_CONNECTIONS_PER_ACCOUNT` | `100` |
| `MOWDECK_REALTIME_MAX_CONNECTIONS_PER_SUBJECT` | `10` |
| `MOWDECK_REALTIME_MAX_SUBSCRIPTIONS_PER_CONNECTION` | `500` |
| `MOWDECK_REALTIME_SEND_QUEUE_SIZE` | `256` |
| `MOWDECK_REALTIME_SEND_TIMEOUT_SECONDS` | `10` |
| `MOWDECK_REALTIME_RECHECK_SECONDS` | `60` |
| `MOWDECK_REALTIME_LAST_STATE_CACHE_SIZE` | `10000` |

### 3.7 Tests

The kit's `lifespan_client` is an httpx client, and httpx's ASGI transport does not speak WebSocket **[unverified, matches httpx 0.28 docs]**. Two levels:

- Protocol and authorization with Starlette's `TestClient(app).websocket_connect(...)` on an app from `make_test_app` (in-memory bus, `InMemoryAuthorizer`, a stub token verifier): auth timeout gives 4408; bad token 4401; service principal 4401; subscribe to own mower gives `subscribed` and the cached `state`; subscribe to a foreign mower gives `error not_found` and no later `state` for it even when the bus publishes one; a state whose `account_id` differs is never forwarded; revocation via a published `authz.tuples_changed` after deleting the tuple gives `unsubscribed access_revoked`; token expiry closes 4401 (injected clock); refresh with another `sub` closes 4401; limits give 4429; oversize frame closes 1009.
- Fan-out and backpressure with uvicorn started in-process on a free port and `websockets.asyncio.client.connect` (websockets 17.2 is locked): two clients on one mower both receive a state; a client that stops reading is closed with 4430 while the other keeps receiving; 1 000 rapid states to a slow client arrive conflated (last one wins); shutdown sends 1012. Optional JetStream test with `MOWDECK_TEST_NATS_URL`: two Realtime apps on one NATS both deliver the same state (no queue group).

---

## 4. Compose wiring and task order

### 4.1 Blocks for `infra/compose/docker-compose.yml`

Generated from `templates/service/template/compose.snippet.yml.jinja`, extended with authz and auth settings. The `MOWDECK_AUTH_*` names are placeholders until Task 6 fixes them **[assumption]**.

```yaml
  fleet-migrate:
    image: mowdeck/fleet:${MOWDECK_VERSION:-dev}
    build: {context: ../.., dockerfile: services/fleet/Dockerfile}
    command: ["python", "-m", "fleet.adapters.db.migrate"]
    read_only: true
    tmpfs: [/tmp]
    environment:
      MOWDECK_DATABASE_URL: postgresql://fleet:${SERVICE_DB_PASSWORD:?set SERVICE_DB_PASSWORD}@postgres:5432/fleet
    depends_on:
      postgres: {condition: service_healthy}
    restart: "no"
    networks: [mowdeck]

  fleet:
    image: mowdeck/fleet:${MOWDECK_VERSION:-dev}
    build: {context: ../.., dockerfile: services/fleet/Dockerfile}
    restart: unless-stopped
    read_only: true
    tmpfs: [/tmp]
    environment: &svc-env
      MOWDECK_ENVIRONMENT: ${MOWDECK_ENVIRONMENT:-dev}
      MOWDECK_LOG_LEVEL: ${MOWDECK_LOG_LEVEL:-INFO}
      MOWDECK_LOG_FORMAT: json
      MOWDECK_NATS_URL: nats://nats:4222
      MOWDECK_DATABASE_URL: postgresql://fleet:${SERVICE_DB_PASSWORD:?set SERVICE_DB_PASSWORD}@postgres:5432/fleet
      MOWDECK_OPENFGA_API_URL: ${OPENFGA_API_URL:-http://openfga:8080}
      MOWDECK_OPENFGA_API_TOKEN: ${OPENFGA_API_TOKEN:-${OPENFGA_AUTHN_PRESHARED_KEYS}}
      MOWDECK_OPENFGA_STORE_NAME: ${OPENFGA_STORE_NAME:-mowdeck}
      MOWDECK_AUTH_ISSUER: ${MOWDECK_AUTH_ISSUER:-http://localhost/api/v1/identity}
      MOWDECK_AUTH_AUDIENCE: mowdeck-api
      MOWDECK_AUTH_JWKS_URL: http://identity:8000/api/v1/identity/.well-known/jwks.json
      MOWDECK_ALLOWED_CONNECTION_KINDS: simulated
    labels:
      - traefik.enable=true
      - traefik.http.routers.fleet.rule=PathPrefix(`/api/v1/fleet`)
      - traefik.http.routers.fleet.entrypoints=web
      - traefik.http.services.fleet.loadbalancer.server.port=8000
    healthcheck: &readyz
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2)"]
      interval: 5s
      timeout: 3s
      retries: 10
      start_period: 5s
    depends_on:
      nats: {condition: service_healthy}
      fleet-migrate: {condition: service_completed_successfully}
      authz-bootstrap: {condition: service_completed_successfully}
    networks: [mowdeck]

  telemetry-migrate:   # same as fleet-migrate with telemetry names

  telemetry:
    # same as fleet with: database telemetry, router telemetry -> /api/v1/telemetry,
    # MOWDECK_HISTORY_RETENTION_DAYS etc., depends_on nats, telemetry-migrate, authz-bootstrap

  realtime:
    image: mowdeck/realtime:${MOWDECK_VERSION:-dev}
    build: {context: ../.., dockerfile: services/realtime/Dockerfile}
    restart: unless-stopped
    read_only: true
    tmpfs: [/tmp]
    environment:
      # kit, OpenFGA and auth variables as for fleet, no database
      MOWDECK_REALTIME_ALLOWED_ORIGINS: ${MOWDECK_REALTIME_ALLOWED_ORIGINS:-}
    labels:
      - traefik.enable=true
      - traefik.http.routers.realtime.rule=PathPrefix(`/api/v1/realtime`)
      - traefik.http.routers.realtime.entrypoints=web
      - traefik.http.services.realtime.loadbalancer.server.port=8000
    healthcheck: *readyz
    stop_grace_period: 15s
    depends_on:
      nats: {condition: service_healthy}
      authz-bootstrap: {condition: service_completed_successfully}
    networks: [mowdeck]
```

YAML anchors are optional; the existing file does not use them, so the implementer may prefer repeating the blocks.

Other changes: remove the `MOWDECK_BOOTSTRAP_MOWERS` value from `.env.example` and the Device-Gateway's environment default stays `[]` (ADR 0013: "must be removed from deployments once Fleet exists"); add `IDENTITY_REGISTRATION_MODE` (name per Task 6) to `.env.example`, commented, with a note that the e2e job sets `open`. The Device-Gateway gets no `depends_on: fleet`: it retries `fleet.mowers.list` with backoff by design.

**What Traefik should depend on.** Today Traefik depends on `authz-bootstrap` only so that `up --wait` accepts the one-shot job (comment in the Compose file). Once Identity, Fleet, Telemetry and Realtime each depend on `authz-bootstrap` and Fleet and Telemetry on their `-migrate` jobs, every one-shot has a dependent and Traefik's dependency should be **removed**: the gateway then starts independently, a failing service does not block routing to the others, which is the plan's "jeder Dienst kann ausfallen" rule. Services become routable as soon as their container runs; Traefik's Docker provider skips containers whose health check reports unhealthy **[unverified for starting containers]**, and `up --wait` waits for all health checks anyway.

```mermaid
flowchart TB
    pg[postgres] --> ofm[openfga-migrate] --> ofga[openfga] --> ab[authz-bootstrap]
    pg --> fm[fleet-migrate] & tm[telemetry-migrate] & im[identity-migrate]
    nats --> dg[device-gateway]
    ab & fm & nats --> fleet
    ab & tm & nats --> telemetry
    ab & im & nats --> identity
    ab & nats --> realtime
    traefik[traefik, no dependencies]
```

### 4.2 Order and definition of done

All three follow the template's definition of done (`docs/dev/service-template.md`: README, tests with cross-tenant cases and at least 85 % coverage, lint, descriptor, Compose block with `compose-check` and a healthy `up --wait`, Helm values, docs, ADR where a decision is made). Specific additions:

**Task 7, Fleet** (needs Task 6 merged for `mowdeck-auth` and the account events; can start with `StaticPrincipalResolver` before that).
Done when: everything in Section 1; `fleet` and `fleet-migrate` in Compose; `.env.example` no longer seeds a mower; the Device-Gateway loads mowers from Fleet in Compose (log "mowers loaded from fleet"); contracts for the command API (Section 6) with regenerated schemas; `docs/architecture/06-runtime-view.md` scenarios updated (Fleet is no longer "future producer"); ADR 0016 "Fleet commands and authorization per command kind" (done); e2e skeleton (Section 5) present with the Fleet-only part green: register, wait for `link_status == connected`, command 202 and `GET /commands/{id}` executed.

**Task 8, Telemetry** (independent of Fleet's code; the end-to-end check needs Fleet).
Done when: Section 2; Compose blocks; retention job with metrics; e2e step "telemetry latest shows docked" green; runtime view "live status" updated.

**Task 9, Realtime and the Phase 1 gate** (needs Task 6's `AuthzTuplesChanged` contract and token-to-principal function).
Done when: Section 3; Compose block; Traefik dependency removed as above; `tests/e2e` complete, `poe e2e` task and CI job `e2e` green on `main`; `docs/phases.md` progress updated and the Phase 1 gate marked met.

---

## 5. Phase 1 acceptance test

### 5.1 Layout and running

```text
tests/e2e/
  e2e_support.py                 # config, HTTP and WebSocket helpers, Actor
  test_phase1_acceptance.py      # fixtures and the scenario
```

No `conftest.py`, following the repository rule (`docs/dev/service-template.md`); pytest's default import mode puts `tests/e2e` on `sys.path`, so the test imports `e2e_support`. The root `testpaths` (`tools`, `libs`, `services`) does not include `tests`, so `poe test` never runs the e2e suite by accident.

Root `pyproject.toml`:

```toml
[dependency-groups]
dev = [ ..., "websockets>=17.2" ]   # already locked through uvicorn[standard]; make it explicit

[tool.poe.tasks.e2e]
help = "Phase 1 acceptance test against a running Compose stack (docker compose up --wait first)"
cmd = "pytest tests/e2e -p no:cacheprovider -o addopts='-ra' --no-cov"

[tool.pytest.ini_options]
markers = ["slow: long-running e2e checks (idle WebSocket through Traefik)"]
```

CI job (`.github/workflows/ci.yml`):

```yaml
  e2e:
    runs-on: ubuntu-24.04
    needs: [python, docker]
    timeout-minutes: 25
    steps:
      - uses: actions/checkout@v7.0.1
      - uses: astral-sh/setup-uv@v10.2.0
        with: {version: "0.12.24", enable-cache: true}
      - run: uv sync
      - name: Configure the stack
        run: |
          cp infra/compose/.env.example infra/compose/.env
          {
            echo "IDENTITY_REGISTRATION_MODE=open"   # two accounts are needed
            echo "MOWDECK_SIM_TIME_SCALE=30"
          } >> infra/compose/.env
      - name: Start the stack
        run: docker compose -f infra/compose/docker-compose.yml up -d --build --wait --wait-timeout 300
      - run: uv run poe e2e
        env:
          MOWDECK_E2E_BASE_URL: http://localhost
      - name: Logs on failure
        if: failure()
        run: docker compose -f infra/compose/docker-compose.yml logs --no-color --timestamps > compose-logs.txt
      - uses: actions/upload-artifact@v4
        if: failure()
        with: {name: compose-logs, path: compose-logs.txt}
      - if: always()
        run: docker compose -f infra/compose/docker-compose.yml down -v
```

### 5.2 The scenario

1. Owner A registers (Identity, open mode) and logs in.
2. Owner A reads the roles and creates sub-user `operator` with the Operator role at account level, and sub-user `viewer` with the Viewer role at account level; both log in.
3. Owner A registers a simulated mower through Fleet (`connection_kind: simulated`, `connection_options: {"time_scale": 30}`).
4. Wait until Telemetry's latest state of that mower is `docked` (the Device-Gateway connected it).
5. The operator opens the WebSocket, authenticates, subscribes to the mower and receives `subscribed`.
6. The operator sends `StartMow` (`area_ids: ["front"]`, an area of the simulator's default map) through Fleet: 202 with a command id.
7. On the WebSocket within `MOWDECK_E2E_LIVE_TIMEOUT`: `command_result` with that id and status `executed`, and a `state` with activity `mowing`. `GET /commands/{id}` shows `executed`.
8. The operator sends `ReturnToDock`: 202, `command_result executed`, a `state` with `returning` or `docked`.
9. Negatives with owner B (second account): 404 on the mower, its commands, a command to it, its telemetry; WebSocket subscribe gives `error not_found` and no `state` for it.
10. Viewer: `StartMow` 403, `EmergencyStop` 202 with `command_result executed` (run last; it stops the mower).
11. No token: 401 on Fleet and Telemetry; WebSocket without `auth` closes with 4408.

The Identity request bodies below follow the identity design 3.6 and 4.3; field names are **[assumptions to align with `services/identity` once merged]**.

### 5.3 Skeleton

```python
# tests/e2e/e2e_support.py
"""Helpers for the Phase 1 acceptance test (runs against the Compose stack through Traefik)."""

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
from websockets.asyncio.client import ClientConnection, connect


@dataclass(frozen=True)
class E2EConfig:
    base_url: str
    ws_url: str
    http_timeout: float
    ready_timeout: float  # mower connected and first telemetry stored
    live_timeout: float  # command result and state on the WebSocket
    run_id: str

    @classmethod
    def from_env(cls) -> "E2EConfig":
        base = os.environ.get("MOWDECK_E2E_BASE_URL", "http://localhost").rstrip("/")
        ws = base.replace("https://", "wss://").replace("http://", "ws://")
        return cls(
            base_url=base,
            ws_url=os.environ.get("MOWDECK_E2E_WS_URL", f"{ws}/api/v1/realtime/ws"),
            http_timeout=float(os.environ.get("MOWDECK_E2E_HTTP_TIMEOUT", "10")),
            ready_timeout=float(os.environ.get("MOWDECK_E2E_READY_TIMEOUT", "60")),
            live_timeout=float(os.environ.get("MOWDECK_E2E_LIVE_TIMEOUT", "20")),
            run_id=uuid4().hex[:8],
        )


@dataclass
class Actor:
    username: str
    password: str
    token: str = ""
    account_id: str = ""
    user_id: str = ""

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


async def register_owner(http: httpx.AsyncClient, cfg: E2EConfig, name: str) -> Actor:
    actor = Actor(username=f"e2e-{cfg.run_id}-{name}", password=f"pw-{uuid4().hex}")
    r = await http.post(
        "/api/v1/identity/register",
        json={
            "username": actor.username,
            "password": actor.password,
            "account_name": actor.username,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    actor.account_id, actor.user_id = body["account_id"], body["user_id"]
    return await login(http, actor)


async def login(http: httpx.AsyncClient, actor: Actor) -> Actor:
    r = await http.post(
        "/api/v1/identity/session", json={"username": actor.username, "password": actor.password}
    )
    assert r.status_code == 200, r.text
    actor.token = r.json()["access_token"]
    return actor


async def create_subuser(
    http: httpx.AsyncClient, cfg: E2EConfig, owner: Actor, name: str, role_key: str
) -> Actor:
    roles_url = f"/api/v1/identity/accounts/{owner.account_id}/roles"
    roles = (await http.get(roles_url, headers=owner.headers)).json()
    role_id = next(r["id"] for r in roles if r["key"] == role_key)
    sub = Actor(username=f"e2e-{cfg.run_id}-{name}", password=f"pw-{uuid4().hex}")
    r = await http.post(
        f"/api/v1/identity/accounts/{owner.account_id}/users",
        headers=owner.headers,
        json={
            "username": sub.username,
            "display_name": name,
            "password": sub.password,
            "must_change_password": False,
            "assignments": [
                {"role_id": role_id, "level": {"type": "account", "id": owner.account_id}}
            ],
        },
    )
    assert r.status_code == 201, r.text
    sub.user_id, sub.account_id = r.json()["id"], owner.account_id
    return await login(http, sub)


async def eventually[T](
    probe: Callable[[], Awaitable[T | None]], timeout: float, what: str, interval: float = 0.5
) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (value := await probe()) is not None:
            return value
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@dataclass
class LiveSocket:
    """A Realtime connection that buffers every message so expectations can look back."""

    ws: ClientConnection
    seen: list[dict[str, Any]] = field(default_factory=list)

    async def send(self, message: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(message))

    async def expect(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float, what: str
    ) -> dict[str, Any]:
        for message in self.seen:
            if predicate(message):
                return message
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=left)
            except TimeoutError:
                break
            message = json.loads(raw)
            self.seen.append(message)
            if predicate(message):
                return message
        raise AssertionError(
            f"no {what} within {timeout}s; got types {[m['type'] for m in self.seen]}"
        )

    async def expect_none(
        self, predicate: Callable[[dict[str, Any]], bool], within: float, what: str
    ) -> None:
        try:
            await self.expect(predicate, within, what)
        except AssertionError:
            return
        raise AssertionError(f"unexpected {what}")


async def open_socket(cfg: E2EConfig, actor: Actor) -> LiveSocket:
    ws = await connect(cfg.ws_url, open_timeout=cfg.http_timeout)
    live = LiveSocket(ws)
    await live.send({"type": "auth", "token": actor.token})
    await live.expect(lambda m: m["type"] == "ready", cfg.http_timeout, "ready")
    return live


def is_type(kind: str, **fields: Any) -> Callable[[dict[str, Any]], bool]:
    def match(m: dict[str, Any]) -> bool:
        return m.get("type") == kind and all(_get(m, k) == v for k, v in fields.items())

    return match


def _get(message: dict[str, Any], dotted: str) -> Any:
    value: Any = message
    for part in dotted.split("__"):
        value = value.get(part) if isinstance(value, dict) else None
    return value
```

```python
# tests/e2e/test_phase1_acceptance.py
"""Phase 1 gate (docs/phases.md): run with `uv run poe e2e` against `docker compose up --wait`."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from e2e_support import (
    Actor,
    E2EConfig,
    LiveSocket,
    create_subuser,
    eventually,
    is_type,
    open_socket,
    register_owner,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

FLEET = "/api/v1/fleet"
TELEMETRY = "/api/v1/telemetry"


@pytest.fixture(scope="module")
def cfg() -> E2EConfig:
    return E2EConfig.from_env()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def http(cfg: E2EConfig) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=cfg.base_url, timeout=cfg.http_timeout) as client:
        r = await client.get("/api/v1/identity/.well-known/jwks.json")
        assert r.status_code == 200, f"stack not reachable through Traefik: {r.status_code}"
        yield client


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def owner_a(http: httpx.AsyncClient, cfg: E2EConfig) -> Actor:
    return await register_owner(http, cfg, "owner-a")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def owner_b(http: httpx.AsyncClient, cfg: E2EConfig) -> Actor:
    return await register_owner(http, cfg, "owner-b")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def operator_a(http: httpx.AsyncClient, cfg: E2EConfig, owner_a: Actor) -> Actor:
    return await create_subuser(http, cfg, owner_a, "operator", "operator")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def viewer_a(http: httpx.AsyncClient, cfg: E2EConfig, owner_a: Actor) -> Actor:
    return await create_subuser(http, cfg, owner_a, "viewer", "viewer")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def mower_a(http: httpx.AsyncClient, cfg: E2EConfig, owner_a: Actor) -> AsyncIterator[str]:
    r = await http.post(
        f"{FLEET}/mowers",
        headers=owner_a.headers,
        json={
            "name": f"e2e-{cfg.run_id}",
            "model": "simulator",
            "connection_kind": "simulated",
            "connection_options": {"time_scale": 30},
        },
    )
    assert r.status_code == 201, r.text
    mower_id: str = r.json()["id"]

    async def docked() -> dict[str, Any] | None:
        s = await http.get(f"{TELEMETRY}/mowers/{mower_id}/state", headers=owner_a.headers)
        if s.status_code == 200 and s.json()["state"]["activity"] == "docked":
            return s.json()
        return None

    await eventually(docked, cfg.ready_timeout, "telemetry state 'docked' for the new mower")
    yield mower_id
    await http.delete(f"{FLEET}/mowers/{mower_id}", headers=owner_a.headers)  # best effort


async def send(
    http: httpx.AsyncClient, actor: Actor, mower_id: str, command: dict[str, Any]
) -> httpx.Response:
    return await http.post(
        f"{FLEET}/mowers/{mower_id}/commands", headers=actor.headers, json={"command": command}
    )


async def test_operator_starts_and_docks_with_live_status(
    http: httpx.AsyncClient, cfg: E2EConfig, operator_a: Actor, mower_a: str
) -> None:
    live = await open_socket(cfg, operator_a)
    try:
        await live.send({"type": "subscribe", "request_id": "s1", "mower_ids": [mower_a]})
        sub = await live.expect(
            is_type("subscribed", request_id="s1"), cfg.live_timeout, "subscribed"
        )
        assert sub["mower_ids"] == [mower_a]

        r = await send(http, operator_a, mower_a, {"kind": "start_mow", "area_ids": ["front"]})
        assert r.status_code == 202, r.text
        start_id = r.json()["command_id"]
        await live.expect(
            is_type("command_result", result__command_id=start_id, result__status="executed"),
            cfg.live_timeout,
            "command_result executed for start_mow",
        )
        await live.expect(
            is_type("state", mower_id=mower_a, state__activity="mowing"),
            cfg.live_timeout,
            "state mowing",
        )
        status = await http.get(f"{FLEET}/commands/{start_id}", headers=operator_a.headers)
        assert status.status_code == 200 and status.json()["status"] == "executed", status.text

        r = await send(http, operator_a, mower_a, {"kind": "return_to_dock"})
        assert r.status_code == 202, r.text
        dock_id = r.json()["command_id"]
        await live.expect(
            is_type("command_result", result__command_id=dock_id, result__status="executed"),
            cfg.live_timeout,
            "command_result executed for return_to_dock",
        )
        await live.expect(
            lambda m: (
                m["type"] == "state"
                and m["mower_id"] == mower_a
                and m["state"]["activity"] in ("returning", "docked")
            ),
            cfg.live_timeout,
            "state returning or docked",
        )
    finally:
        await live.ws.close()


FOREIGN_ROUTES = [
    ("GET", f"{FLEET}/mowers/{{m}}", None),
    ("PATCH", f"{FLEET}/mowers/{{m}}", {"name": "stolen"}),
    ("DELETE", f"{FLEET}/mowers/{{m}}", None),
    ("PUT", f"{FLEET}/mowers/{{m}}/groups", {"group_ids": []}),
    ("GET", f"{FLEET}/mowers/{{m}}/commands", None),
    (
        "POST",
        f"{FLEET}/mowers/{{m}}/commands",
        {"command": {"kind": "start_mow", "area_ids": ["front"]}},
    ),
    ("POST", f"{FLEET}/mowers/{{m}}/commands", {"command": {"kind": "emergency_stop"}}),
    ("GET", f"{TELEMETRY}/mowers/{{m}}/state", None),
    ("GET", f"{TELEMETRY}/mowers/{{m}}/history", None),
    ("GET", f"{TELEMETRY}/mowers/{{m}}/positions", None),
    ("GET", f"{TELEMETRY}/mowers/{{m}}/commands", None),
]


@pytest.mark.parametrize(("method", "path", "body"), FOREIGN_ROUTES)
async def test_foreign_owner_gets_404_everywhere(
    http: httpx.AsyncClient, owner_b: Actor, mower_a: str, method: str, path: str, body: Any
) -> None:
    r = await http.request(method, path.format(m=mower_a), headers=owner_b.headers, json=body)
    assert r.status_code == 404, f"{method} {path}: {r.status_code} {r.text}"


async def test_foreign_owner_never_lists_the_mower(
    http: httpx.AsyncClient, owner_b: Actor, mower_a: str
) -> None:
    r = await http.get(
        f"{FLEET}/mowers", headers=owner_b.headers, params={"account_id": "smuggled"}
    )
    assert r.status_code == 200
    assert mower_a not in {m["id"] for m in r.json()["items"]}


async def test_foreign_owner_cannot_subscribe(cfg: E2EConfig, owner_b: Actor, mower_a: str) -> None:
    live = await open_socket(cfg, owner_b)
    try:
        await live.send({"type": "subscribe", "request_id": "x", "mower_ids": [mower_a]})
        err = await live.expect(is_type("error", request_id="x"), cfg.live_timeout, "error")
        assert err["code"] == "not_found" and mower_a in err["mower_ids"]
        await live.expect_none(is_type("state", mower_id=mower_a), within=12, what="foreign state")
    finally:
        await live.ws.close()


async def test_requests_without_token_are_401(
    http: httpx.AsyncClient, cfg: E2EConfig, mower_a: str
) -> None:
    assert (await http.get(f"{FLEET}/mowers/{mower_a}")).status_code == 401
    assert (await http.get(f"{TELEMETRY}/mowers/{mower_a}/state")).status_code == 401
    live = LiveSocket(await connect(cfg.ws_url, open_timeout=cfg.http_timeout))
    with pytest.raises(ConnectionClosed) as closed:
        await live.expect(lambda m: False, timeout=10, what="close")  # no auth message sent
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4408


async def test_viewer_cannot_start_but_can_emergency_stop(  # keep last: it stops the mower
    http: httpx.AsyncClient, cfg: E2EConfig, viewer_a: Actor, mower_a: str
) -> None:
    r = await send(http, viewer_a, mower_a, {"kind": "start_mow", "area_ids": ["front"]})
    assert r.status_code == 403, r.text
    r = await send(http, viewer_a, mower_a, {"kind": "emergency_stop"})
    assert r.status_code == 202, r.text
    stop_id = r.json()["command_id"]

    async def executed() -> str | None:
        s = await http.get(f"{FLEET}/commands/{stop_id}", headers=viewer_a.headers)
        return (
            s.json()["status"]
            if s.status_code == 200 and s.json()["status"] == "executed"
            else None
        )

    await eventually(executed, cfg.live_timeout, "emergency stop executed")


@pytest.mark.slow
async def test_idle_socket_survives_traefik(cfg: E2EConfig, operator_a: Actor) -> None:
    live = await open_socket(cfg, operator_a)
    try:
        await live.expect_none(lambda m: m["type"] == "never", within=90, what="nothing")
        await live.send({"type": "ping", "request_id": "p"})
        await live.expect(is_type("pong", request_id="p"), cfg.live_timeout, "pong after 90 s idle")
    finally:
        await live.ws.close()
```

The `slow` test runs in CI too (90 s is acceptable in a 25-minute job); locally `-m "not slow"` skips it. Test order matters only for the viewer test (it stops the mower), which is last in the file; pytest keeps file order.

### 5.4 Troubleshooting

| Symptom | Likely cause and check |
| --- | --- |
| `http` fixture fails, stack not reachable | Traefik not on port 80 (`GATEWAY_HTTP_PORT` in `.env`), or Identity not healthy: `docker compose ps`, `docker compose logs identity`. Set `MOWDECK_E2E_BASE_URL=http://localhost:<port>`. |
| Second `register` returns 403 | Identity runs in `closed` mode; the e2e stack needs open registration (Section 5.1). |
| Mower never reaches `docked` | Device-Gateway did not get the registration: check `docker compose logs device-gateway` for "mower registered" and "mowers loaded from fleet"; Fleet answered 503 on registration (OpenFGA unhealthy); Telemetry consumer not running (`/readyz` 503) or dropping states because the directory account differs (warning in Telemetry logs). |
| 404 for the owner on their own mower | Tuples missing: `fga tuple read --object mower:<id>` in the OpenFGA CLI; FGA model not bootstrapped (`authz-bootstrap` logs); services pinned an old model (restart after a model change, `mowdeck-authz` README). |
| Operator gets 403 on `start_mow` | Role assignment not at account level or Identity's reconcile pending: `GET /api/v1/identity/role-assignments?subject=...`; tuples `user:<op>#grant_mower_control@account:<A>` and `#member`. Up to 5 s cache TTL after a change. |
| `command_result` never arrives | Gateway rejected it: `docker compose logs device-gateway` ("command rejected", reason `unknown mower` or account mismatch); command expired (clock skew or TTL too short); Realtime subscribed after the result (results are not cached, only states). |
| `state mowing` never arrives but result is `executed` | Realtime fan-out: check `realtime` logs for slow-consumer closes (4430); simulator finished the area too fast with a very high `MOWDECK_SIM_TIME_SCALE` (keep 30); heartbeat every 10 s should still deliver the current state. |
| WebSocket closes with 4401 | Token expired (10 minutes) or issuer/audience mismatch between Identity and `MOWDECK_AUTH_*`. |
| WebSocket closes with 4408 | The client did not send `auth` within 5 s. |
| Idle test fails at about 60 s | Traefik responding timeouts cut upgraded connections: set `--entrypoints.web.transport.respondingTimeouts.readTimeout=0` for the stack and record the finding in the Realtime README. |
| Flaky timing in CI | Raise `MOWDECK_E2E_LIVE_TIMEOUT` and `MOWDECK_E2E_READY_TIMEOUT`; never add sleeps. |
| Leftover data locally | Usernames are unique per run; `docker compose down -v` resets everything. |

---

## 6. Contracts to add

All additions are new modules or optional fields in `mowdeck_contracts.v1` (backward compatible, ADR 0009), with JSON Schemas regenerated into `libs/mowdeck-contracts/schemas/v1/`. REST resource models of Fleet (`Site`, `Group`, `Mower` views) stay in the service's OpenAPI; only models shared across services or with external clients (web app, HACS integration, e2e) go into contracts.

**Fleet events.** No new event types for Phase 1. `site.*` and `group.*` events have no consumer yet (Realtime re-checks via `authz.tuples_changed`); add them in Phase 4 with the fleet map. Optional paging for the registry request (consumers ignore unknown fields):

```python
class MowerListRequest(ContractModel):  # fleet.py, new optional fields
    shard_index: int = 0
    shard_count: int = 1
    after: UUID | None = None  # NEW: return mowers with id > after
    limit: int | None = Field(default=None, ge=1, le=5000)  # NEW


class MowerListReply(ContractModel):
    mowers: list[MowerRegistration] = []
    next_after: UUID | None = None  # NEW: set when more pages exist
```

**Consumed from Task 6** (fields this design relies on): `AccountCreated {account_id, kind}`, `AccountDeleted {account_id}`, `AuthzTuplesChanged {subjects: list[str], objects: list[str], hierarchy: bool}` (strings in OpenFGA form, `hierarchy` true for Fleet's `account` and `parent` writes).

**Command API** (`mowdeck_contracts.v1.commands`, new):

```python
class CommandRequest(ContractModel):
    command: MowerCommand
    ttl_seconds: int | None = Field(default=None, ge=1, le=300)


class CommandState(StrEnum):
    PENDING = "pending"  # published, no result yet
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXPIRED = "expired"
    FAILED = "failed"


class CommandAccepted(ContractModel):
    command_id: UUID
    mower_id: UUID
    kind: str
    priority: CommandPriority
    issued_at: UtcDateTime
    expires_at: UtcDateTime
    correlation_id: CorrelationId
    status_url: str  # /api/v1/fleet/commands/<id>


class CommandView(ContractModel):
    command_id: UUID
    mower_id: UUID
    bulk_id: UUID | None = None
    kind: str
    priority: CommandPriority
    state: CommandState
    reason: str | None = Field(default=None, max_length=500)
    issued_by: PrincipalRef
    issued_at: UtcDateTime
    expires_at: UtcDateTime
    updated_at: UtcDateTime


class BulkCommandItem(ContractModel):
    mower_id: UUID
    accepted: bool
    command_id: UUID | None = None
    reason: Literal["forbidden", "not_published"] | None = None


class BulkCommandAccepted(ContractModel):
    bulk_id: UUID
    target_type: Literal["site", "group"]
    target_id: UUID
    items: list[BulkCommandItem]
```

**Realtime messages** (`mowdeck_contracts.v1.realtime`, new):

```python
RequestId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,64}$")]


# client -> server
class RtAuth(ContractModel):
    type: Literal["auth"] = "auth"
    token: str = Field(min_length=1, max_length=4096)


class RtSubscribe(ContractModel):
    type: Literal["subscribe"] = "subscribe"
    request_id: RequestId | None = None
    mower_ids: list[UUID] = Field(default_factory=list, max_length=500)
    all_visible: bool = False


class RtUnsubscribe(ContractModel):
    type: Literal["unsubscribe"] = "unsubscribe"
    request_id: RequestId | None = None
    mower_ids: list[UUID] = Field(max_length=500)


class RtPing(ContractModel):
    type: Literal["ping"] = "ping"
    request_id: RequestId | None = None


RealtimeClientMessage = Annotated[
    RtAuth | RtSubscribe | RtUnsubscribe | RtPing, Field(discriminator="type")
]


# server -> client
class RtReady(ContractModel):
    type: Literal["ready"] = "ready"
    connection_id: UUID
    subject: UUID
    account_id: UUID
    token_expires_at: UtcDateTime
    heartbeat_seconds: int


class RtSubscribed(ContractModel):
    type: Literal["subscribed"] = "subscribed"
    request_id: RequestId | None = None
    mower_ids: list[UUID]


class RtUnsubscribed(ContractModel):
    type: Literal["unsubscribed"] = "unsubscribed"
    request_id: RequestId | None = None
    mower_ids: list[UUID]
    reason: Literal["requested", "access_revoked", "mower_removed"]


class RtState(ContractModel):
    type: Literal["state"] = "state"
    mower_id: UUID
    state: MowerState


class RtCommandResult(ContractModel):
    type: Literal["command_result"] = "command_result"
    mower_id: UUID
    result: CommandResult


class RtErrorCode(StrEnum):
    INVALID_MESSAGE = "invalid_message"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    LIMIT_EXCEEDED = "limit_exceeded"
    INTERNAL = "internal"


class RtError(ContractModel):
    type: Literal["error"] = "error"
    request_id: RequestId | None = None
    code: RtErrorCode
    message: str = Field(max_length=500)
    mower_ids: list[UUID] = Field(default_factory=list)


class RtPong(ContractModel):
    type: Literal["pong"] = "pong"
    request_id: RequestId | None = None


RealtimeMessage = Annotated[
    RtReady | RtSubscribed | RtUnsubscribed | RtState | RtCommandResult | RtError | RtPong,
    Field(discriminator="type"),
]

REALTIME_CLOSE_CODES: Final = {
    4400: "protocol error",
    4401: "unauthorized",
    4408: "auth timeout",
    4429: "too many connections",
    4430: "slow consumer",
}
```

The brief names four server messages (`state`, `command_result`, `subscribed`, `error`); `ready`, `unsubscribed` and `pong` are needed for authentication, revocation and client-side liveness.

**Telemetry queries** (`mowdeck_contracts.v1.telemetry`, new):

```python
class MowerStateSnapshot(ContractModel):
    state: MowerState
    received_at: UtcDateTime
    stale: bool


class HistoryBucket(StrEnum):
    RAW = "raw"
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    D1 = "1d"


class HistoryQuery(ContractModel):  # query parameters of /history and /positions
    from_: UtcDateTime = Field(alias="from")
    to: UtcDateTime
    bucket: HistoryBucket = HistoryBucket.M5


class StateHistoryPoint(ContractModel):
    at: UtcDateTime  # bucket start (or the sample time for raw)
    activity: MowerActivity  # last in bucket
    battery_percent: int = Field(ge=0, le=100)
    battery_min: int = Field(ge=0, le=100)
    battery_max: int = Field(ge=0, le=100)
    charging: bool
    rtk_fix: RtkFixStatus
    error_code: str | None = None
    samples: int = Field(ge=1)


class StateHistory(ContractModel):
    mower_id: UUID
    from_: UtcDateTime = Field(alias="from")
    to: UtcDateTime
    bucket: HistoryBucket
    points: list[StateHistoryPoint]
    truncated: bool = False


class PositionPoint(ContractModel):
    at: UtcDateTime
    position: GeoPosition
    rtk_fix: RtkFixStatus
    rtk_source: RtkSource


class PositionTrack(ContractModel):
    mower_id: UUID
    points: list[PositionPoint]
    truncated: bool = False


class CommandLogEntry(ContractModel):
    command_id: UUID
    status: CommandStatus
    reason: str | None = None
    at: UtcDateTime


class CommandLog(ContractModel):
    mower_id: UUID
    entries: list[CommandLogEntry]
    next_cursor: str | None = None
```

`ContractModel` is frozen and ignores unknown fields (`_base.py`); the `from` alias needs `populate_by_name` on these two models.

---

## 7. Open questions for the owner

1. **Two command records.** Proposed: Fleet keeps the authoritative command record (who ordered what, latest state, 90 days; it is the plan's command audit), Telemetry keeps a per-mower timeline of result transitions (30 days) for history views and later reports. Alternative: only Fleet, and Telemetry's `/commands` route is dropped. The duplication is small but is still two sources for "what happened to command X".
2. **Bulk emergency stop.** Proposed: stopping every mower of a group or site needs only view rights on the target and on each mower, consistent with `can_emergency_stop = can_view` (identity design, open question 1). Alternative: require `fleet.bulk_command` for bulk stops too. Safety argues for the former.
3. **Serial number uniqueness.** Proposed for Phase 1: unique per account, so a "serial already registered" answer never reveals another account's device. When real devices are claimed in Phase 2, global uniqueness with an explicit claim and transfer flow may be needed to stop the same LUBA being registered twice.
4. **Retention of positions.** Positions reveal where a household's garden is and when someone is at home. Proposed defaults: state history 30 days, positions 90 days (the RTK map needs a season of data to be useful), command records 90 days, all configurable per installation. Should positions be shorter by default, or opt-in per account?
