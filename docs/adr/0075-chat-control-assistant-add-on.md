# ADR 0075: Chat control as an optional assistant add-on

## Status

Accepted (2026-11-01).

## Context

Users want to tell MowDeck what to do in their own words: "mäh morgen früh nur hinten", "schick ihn zur Station", "pausier bis 15 Uhr". The AI provider a user configured under Settings, AI provider ([ADR 0033](0033-vision-assisted-recovery-with-approval.md)) already reads camera pictures for AI assist; the same provider can read a sentence.

Binding requirements of the owner:

1. **An optional add-on, not woven into the core.** Its own service, off unless enabled (Compose profile, Helm value), its web UI only where it is deployed. The core services neither import nor know it, and removing it leaves everything else working. It talks to the rest only through public contracts with the caller's own authorization: no new privileged path.
2. **Provider neutral and capability aware.** The model may only pick from the mower's real actions and areas and the actions the caller may use; its answer is validated server side, and user text and model output are untrusted (prompt injection must not widen the action set).
3. **Nothing runs without confirmation.** A confirmed proposal goes through the existing command and schedule paths, so authorization per command kind ([ADR 0016](0016-fleet-commands-and-authorization-per-command-kind.md)), plans, the controller's safety rules ([ADR 0012](0012-mower-control-interface-and-safety-rules.md)) and the audit apply unchanged.
4. **No manual driving by chat, no settings by chat** in this version.
5. **Demo mode** ([ADR 0068](0068-public-demo-mode.md)) calls no external AI.

### Options

**Where the feature lives.**

1. Inside Vision or Fleet: least code, but it couples a convenience feature to core services and violates requirement 1.
2. A new service on the bus (its own NATS user, ADR 0074) that reads mower state from NATS and issues commands through `fleet.commands.issue` on the user's behalf: it would need a NATS user with request rights on Fleet's internal subjects, which is exactly a privileged path that bypasses the public API.
3. **A new HTTP-only service** that reads the public REST API with the caller's bearer token (the token the web app sent it), like any other client.

**How it reuses the AI provider.**

1. Its own provider settings: a second place for keys and a second settings page.
2. A shared library with the LiteLLM client, plus a way to fetch the user's decrypted key from Vision: keys would leave Vision, and the fetch would be a privileged back door.
3. **A defined interface on Vision**: a generic, text-only "structured answer" endpoint on the caller's own effective provider. Vision keeps the keys, the resolution (own provider, else the installation's), the limits and the cost accounting.

**Who executes a confirmed proposal.**

1. The assistant, after a "confirm" call: it would need write paths and become a second place that issues commands.
2. **The web app**, from the structured actions of the proposal, through the same endpoints its buttons use.

## Decision

Option 3, option 3 and option 2: **a separate, HTTP-only, read-only service `services/assistant`** (created from `templates/service`), **Vision's new `POST /vision/me/provider/structured`** for the model call, and **execution by the web app** after the user confirmed.

### Architecture

```
web app ──POST /assistant/mowers/{id}/proposals (user token)──▶ assistant
                                                                 │ same token, public API only
           ┌─────────────────────────────┬──────────────────────┼────────────────────────────┐
           ▼                             ▼                      ▼                            ▼
 Identity POST /me/permissions   Fleet GET /mowers/{id}   Telemetry GET map,       Vision POST /me/provider/
 (can_view, can_control,         (name, area names)       capabilities, state      structured (the user's
  can_edit_schedule)                                                               provider; no key leaves)
web app ◀── proposal (validated actions) ── assistant
web app ── user confirms ──▶ Fleet POST /mowers/{id}/commands | Scheduler POST /schedules (kind once)
```

- **No NATS user, no database, no secret.** The assistant sets `MOWDECK_NATS_ENABLED=false` and gets none of `MOWDECK_NATS_URL`, `MOWDECK_DATABASE_URL` or OpenFGA settings. So ADR 0074's permission map is unchanged: an HTTP-only service needs no NATS user, which is the better choice than adding one (no `NATS_PASSWORD_ASSISTANT`, nothing to rotate, no subject it could misuse).
- **The caller's own authorization.** The assistant verifies the bearer token locally (JWKS, like every service) and forwards it unchanged to Identity, Fleet, Telemetry and Vision inside the network. It can never see or do more than the user can in the web app.
- **The core does not know it.** No core service imports it or names its API. Identity lists its descriptor only through `IDENTITY_EXTRA_MODULE_URLS` (Compose) or when `services.assistant.enabled` (Helm, the chart's own wiring); the web app shows the chat box only when Identity announces the assistant's UI area `/assistant` and the assistant answers `GET /assistant/status`. `tools/repo-checks/tests/test_addon_isolation.py` enforces all of this.
- **Two generic core additions**, both useful without the assistant and neither mentioning it: Vision's structured-answer endpoint and the Scheduler's one-off schedule kind (below).

### The actions

| Kind | After confirmation the web app sends | Relations (checked by the catalog, again by the endpoint) |
| --- | --- | --- |
| `start_now` | `POST /fleet/mowers/{id}/commands` `start_mow` with `area_ids` | `can_control` |
| `start_later` | `POST /scheduler/schedules`, `kind: once`, in the user's time zone | `can_control` and `can_edit_schedule` |
| `pause` | `pause_mow` | `can_control` |
| `resume` | `resume_mow` | `can_control` |
| `dock` | `return_to_dock` | `can_control` |

That is the whole list. Manual driving, spot runs, the emergency stop, firmware and every setting are not kinds, so neither the model nor the validator can produce them.

### Catalog, schema and prompt

For each request the assistant builds a **catalog** from the mower's real state on the caller's token: the kinds the caller may use (relations), only when the mower ever connected (it reported capabilities), starts only with map areas, immediate kinds only in a fitting activity (`pause` while mowing or returning, `resume` while paused, `dock` unless docked or charging, `start_now` when idle, docked or charging; an unknown or offline activity offers everything and leaves the decision to the controller). The areas carry their user-visible names (Fleet's custom name, else the mower's).

From the catalog it generates a **strict JSON schema** (every object closed, every property required): `kind` is an `enum` of the offered kinds, `area_ids` an `enum` of the mower's area ids, plus `start_at` (local `YYYY-MM-DDTHH:MM`), `duration_minutes` and `notes` (an `enum`: `manual_drive`, `settings`, `timed_resume`, `other_mower`, `unsupported`, `unclear`). The answer has **no free text**: nothing the model writes is ever shown or executed except these codes.

The request goes to the model as **one JSON document** in the user message (request, mower, areas, offered kinds, local time, time zone). The system prompt declares everything in it data and never instructions. With nothing offered (a viewer, a never connected mower) the model is not asked at all.

### Validation

Whatever the model returned (a strict-schema model, a prompt-mode model, or one that followed injected text), `assistant.domain.validate` checks it against the catalog again:

- the shape (closed objects, strict types, at most 10 actions and notes in the parse);
- every kind is known and offered; areas exist (duplicates dropped, at most 50; an empty list means every area); immediate kinds carry no areas, time or duration;
- a later start has a valid local time at least 2 minutes and at most 7 days ahead, converted to UTC in the user's time zone, and a window of 15 to 720 minutes (default 120);
- at most 3 actions, at most one immediate action and two later starts.

**All or nothing**: any rejected action leaves the proposal without executable actions, so a partly understood request never runs half of what the user meant. Unknown kinds are never echoed back.

### Confirmation and execution

The proposal carries the validated actions with area names, the local and UTC start, and `expires_at` (5 minutes). The web app shows them as a **work order** (action, mower, areas, time, window, notes) with "Confirm and run" and "Discard". Only after confirmation does it send each action in order through Fleet or the Scheduler with the user's token, stopping at the first refusal; an expired proposal is never sent. Every executing endpoint authorizes the user again, applies plans and the controller's rules, and records its audit exactly as for a button press.

### Vision's structured answers

`POST /vision/me/provider/structured` (`purpose`, `name`, `system`, `text`, `schema`) answers one JSON object from the caller's effective provider: the same resolution as AI assist (own provider, else the installation's with the plan feature `ai_recover_hosted`, else 409 `vision-provider-missing`), its own hourly limit per user (`VISION_STRUCTURED_PER_HOUR`, default 30), at most 2000 output tokens, recorded in the cost accounting as kind `structured` with the purpose, never the text. LiteLLM gets the schema as strict structured output where the model supports it, else in the prompt (one retry when the answer is no JSON object). The fake family (`fake/...`) answers the smallest object the schema allows, so development stacks see the shape without a model. Vision validates only that the answer is an object; its content is the caller's to validate.

No shared library was extracted: model calls stay in Vision, the only service that holds provider keys. If another service ever needs its own model calls, extracting the LiteLLM client into a library is the later step.

### One-off schedules

A later start needs a schedule that runs once. The Scheduler gets the recurrence kind `once` (`anchor_date` is the day; `next_run_at` is null after it ran; creating one in the past is refused). The schedule form offers it too ("Once"), and schedule lists show "Once on <date>".

### Interpreters, demo mode and tests

`MOWDECK_ASSISTANT_INTERPRETER=provider` (default) reads requests with the user's AI provider; `phrases` uses a small deterministic **phrase parser** (German and English: later starts with day and time words, "nur hinten", dock, pause with "until", resume; driving and settings phrases produce their notes). Its output goes through the same validator. **Demo mode always uses the phrase parser**, so a demo never calls an external AI. The demo stack itself does not run the add-on (demo stacks take no Compose profiles, ADR 0068); an operator can run it there with the phrase parser.

The unit tests use a fake AI provider that returns scripted answers, hostile ones included; the HTTP adapters are tested against a mocked public API; the Playwright spec runs the confirm flow against the e2e stack, where the assistant runs with the phrase parser. No test calls a real AI API.

### No provider

`GET /assistant/status` asks Vision for the caller's effective provider. Without one the chat box explains that chat control reads requests with the user's AI provider and links to Settings, AI provider.

### Not in this version

- **Manual driving by chat**: never. Driving needs the live view and a hand on the stop; a sentence cannot provide either.
- **Settings by chat** (blade height, speed, patterns, mowing defaults, recurring schedules): a later option. It would add kinds that map to the existing settings endpoints with the same confirmation card; the validation would need each setting's range from the capabilities.
- **Resuming at a time** ("pause until 15:00"): the pause is offered, the timed resume is explained as not possible yet. A later option is a Scheduler action `resume` for one-off schedules.
- Several mowers in one request, conversation memory, voice input.

## Consequences

- The feature is one optional container: `COMPOSE_PROFILES=assistant` plus `http://assistant:8000` in `IDENTITY_EXTRA_MODULE_URLS` (Compose), `services.assistant.enabled=true` (Helm). Without it nothing changes, and no chat box appears.
- The assistant holds no state and no credentials; it scales and restarts freely. Its rate limit (`ASSISTANT_REQUESTS_PER_MINUTE`) is per replica; Vision's hourly limit bounds the AI cost per user.
- The two core additions are generic: any client may ask Vision for a structured answer with its own provider (a user spends their own key, or the installation's within the plan and the hourly limit) and create one-off schedules.
- A model that misreads a request can only propose wrong actions from the offered list; the user sees the exact actions before anything runs, and the card says that the assistant can be wrong.
- Every new kind needs its row in the table above, its validation, its execution mapping in the web app and tests for hostile answers.
