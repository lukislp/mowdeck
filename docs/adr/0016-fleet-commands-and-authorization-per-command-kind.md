# ADR 0016: Fleet commands and authorization per command kind

## Status

Accepted (2026-10-09)

## Context

Fleet (Task 7 of Phase 1) owns sites, groups and mowers, places them in the hierarchy `account > site > group > mower` of the authorization model ([ADR 0014](0014-authorization-model-and-tuple-ownership.md)) and is the only service that sends commands to mowers. The Device-Gateway executes them and publishes results ([ADR 0012](0012-mower-control-interface-and-safety-rules.md), [ADR 0013](0013-device-gateway-registry-and-sharding.md)); envelopes, subjects and de-duplication are fixed by [ADR 0009](0009-command-and-event-envelopes.md).

Open points the design ([Design: fleet, telemetry, realtime](../dev/design-fleet-telemetry-realtime.md), section 1) left to this task:

- Which right does a command need? The model has `can_control`, `can_drive` and `can_emergency_stop` (which equals `can_view`), and the plan wants anybody who sees a mower to be able to stop it.
- Where is the check, and what must not happen before it?
- Who keeps the record of a command (who, what, when, outcome), which the plan asks for as the command audit?
- How does a command for a whole group or site behave when the caller may command only some of its mowers?
- Where does a mower go when it is registered without a site, given that the model needs a `parent` site for every mower and the account event can arrive after the first registration?
- How do the database and OpenFGA stay consistent: the design proposed an outbox, Identity implemented a write-through instead ([ADR 0015](0015-tokens-sessions-and-key-rotation.md), Identity README).

## Decision

**The relation depends on the command kind.** `start_mow`, `pause_mow`, `resume_mow`, `return_to_dock`, `set_blade_height` and `set_speed` need `can_control`; `manual_drive` needs `can_drive`; `emergency_stop` needs `can_emergency_stop`, which equals `can_view`. The table lives in `fleet.domain.rules.COMMAND_RELATION`; an unknown kind is never allowed. Emergency stops always use their own short expiry (`MOWDECK_EMERGENCY_STOP_TTL_SECONDS`, default 10 s), manual drive frames 2 s, other kinds the requested expiry capped at `MOWDECK_COMMAND_TTL_MAX_SECONDS`.

**The check runs before any I/O.** `POST /api/v1/fleet/mowers/{id}/commands` parses the body, derives the relation and asks OpenFGA through `mowdeck_authz.fastapi.authorize()` (a new library helper for relations chosen at runtime, same answers as `require_or_not_found`): 403 when the caller sees the mower, 404 when not. Only then is the mower row loaded (with the account filter), the envelope built and the record written. A refused command never reaches the database or NATS (tested). The envelope takes `issued_by` from the verified principal and `account_id` from the mower row, never from the request.

**Fleet keeps the command record.** Every accepted command gets a row in `commands` (kind, command, priority, issued by, issued and expiry time, correlation id, status, reason, time of the last result), inserted and committed before the command is published (msg id = command id). A durable consumer shared by all replicas (`fleet-command-results` on `command.result.mower.*`) moves the status forward only: `pending < accepted < {executed, rejected, expired, failed}` as one conditional update, so redeliveries and out-of-order results change nothing. If the bus cannot take the command the record becomes `failed` and the API answers 503; there is no retry, because a late start is worse than none. `GET /api/v1/fleet/commands/{id}` reads the record (account filter plus `can_view` on the mower, 404 otherwise). Records are kept `MOWDECK_COMMAND_RETENTION_DAYS` (default 90) and purged by a job that one replica runs at a time (`pg_try_advisory_xact_lock`).

**Bulk commands are checked per mower.** `POST /groups/{id}/commands` and `/sites/{id}/commands` first need `can_bulk_command` on the target (for an emergency stop only `can_emergency_stop`, i.e. view, so whoever sees a group can stop all of it). Then one batch check asks the command's relation and `can_view` for every member: allowed mowers get their own envelope and record with a shared `bulk_id`, visible but not allowed mowers are reported `forbidden`, invisible mowers are omitted so a group never reveals them. The answer (`BulkCommandAccepted`) has one item per visible mower.

**A hidden default site per account.** Fleet creates it when `identity.account.created` arrives (durable consumer `fleet-accounts`) and lazily when a mower is registered without a site, in the same transaction. A partial unique index (`uq_sites_one_default`) resolves a race between the two. Lists hide it unless `include_default=true`; it cannot be renamed or deleted.

**Tuples are written inside the transaction (no outbox).** Like Identity, a change runs in one database transaction; right before the commit Fleet writes the tuples through `TupleWriter(owner="fleet")` (only `site#account`, `group#account/parent`, `mower#account/parent`). If OpenFGA cannot be written, the transaction rolls back and the request answers 503: nothing is half applied and no event is published. If the commit fails after the write, the tuple change is undone (best effort, logged). Events (`mower.registered`, `mower.updated`, `mower.removed`) are published after the commit, so the Device-Gateway only connects a mower whose tuples exist. An outbox was rejected for now: the write-through gives the immediate 503 the plan asks for, keeps one consistency pattern across the two tuple writers and avoids a background drain whose delay would make a new mower invisible for a moment.

## Consequences

- A Viewer can stop a mower but not start it; a foreign account learns nothing (404 for every route and command).
- Command history and current status come from Fleet alone; Telemetry may keep a result timeline for charts later without being the authority.
- Registering mowers and changing the hierarchy needs OpenFGA to be up (503 otherwise); sending commands needs OpenFGA for the check and NATS for the delivery.
- A failed event publish after a committed change is logged and not retried; the Device-Gateway catches up on its next start through `fleet.mowers.list`. If both the commit and the undo fail, OpenFGA may keep a tuple for an object that does not exist, which is harmless because every handler also loads the row with the account filter. A reconcile job like Identity's is follow-up work.
- Other services see hierarchy changes (a deleted or moved mower) after at most their decision cache TTL (5 s); Fleet does not publish a cache invalidation event for them.
- `mowdeck-authz` gained `authorize()`; services with body-dependent relations use it instead of copying the dependency logic.

## Amendment (2026-10-14): path previews are relayed as a core NATS request, with ordered timeouts

**Decision.** `POST /api/v1/fleet/mowers/{id}/path-preview` lets a mower plan the route of a start without starting it ([ADR 0012](0012-mower-control-interface-and-safety-rules.md), amendment of the same date). It is not a command: Fleet writes no command record and no audit row, publishes nothing on the `COMMAND` stream and sends no envelope. Instead Fleet sends one core NATS request `PathPreviewGatewayRequest{mower_id, account_id, area_ids, pattern, requested_by}` on `gateway.mower.<id>.path.preview` (an internal subject outside every stream, like `fleet.mowers.list`) and answers the caller with the `PathPreviewReply`: the `MowPath` (polylines in the map's local metres, `kinds`, `points_total`, `path_hash`, no progress), or an error. Every Device-Gateway instance subscribes without a queue group; only the shard that holds the mower answers, the others stay silent, so Fleet gets exactly one reply or a timeout. Nothing is stored: the start afterwards names the same areas and pattern, and the mower plans the same route again.

**Authorization and gating.** The route needs `can_control` on the mower with the 404 semantics of every mower route (a foreign account learns nothing), not `can_view`: planning makes the mower compute and is part of starting it. It also needs the plan feature `control` (403 `feature-not-in-plan`), like every start.

**Errors.** The reply's `error.code` maps to one problem each: `unsupported` to 409 `urn:mowdeck:problem:path-preview-unsupported`, `rejected` (a job runs, unknown areas, a pattern the mower cannot run) to 409 `path-preview-rejected` with the reason as `detail`, `timeout` and no reply in time to 504 `gateway-timeout`, `unavailable`, `not_found` (no gateway holds the mower) and `failed` to 503, `forbidden` to 404.

**Timeouts are ordered, innermost shortest.** The gateway bounds the mower's planning with `MOWDECK_PATH_PREVIEW_TIMEOUT_SECONDS` (75 s) and then answers the error `timeout`; Fleet waits `MOWDECK_PATH_PREVIEW_REQUEST_TIMEOUT_SECONDS` (85 s) for a reply and answers 504 when none comes; the web app allows 90 s for the request (`SLOW_REQUEST_TIMEOUT_MS`). The order gateway < Fleet < client is a contract of the deployment: Compose (`infra/compose`) and the Helm chart (`infra/helm/mowdeck`) set both values next to each other with the rule written down, so each layer reports its own, specific timeout instead of a generic one of the layer above (a gateway `timeout` says "the mower did not finish", a 504 says "no gateway answered").

Alternatives: a command with a result event would force the browser to wait on the Realtime socket for an answer to a question and would leave command rows for something that changed nothing. A request to the Planner would answer with a route of `mowdeck-patterns`, not the one the LUBA drives (ADR 0012, "Native jobs first"). A JetStream subject would be acknowledged by the stream instead of the responder (the lesson of `tracks.positions.query`, [ADR 0030](0030-mowing-patterns-and-planner.md)).

Consequences: previews need NATS and a gateway that holds the mower (503 otherwise); they are not in the command history. A deployment that changes one timeout has to keep the order; the settings' documentation and the deployment files say so. Over 4G a preview can take most of its budget; clients show progress.
