# vision

AI assist for stuck mowers (Ai Recover) and Find Dock with human approval ([ADR 0033](../../docs/adr/0033-vision-assisted-recovery-with-approval.md)): a camera snapshot and the map around the mower go to a vision model, the model proposes a short maneuver, a user who may drive the mower approves it, and the service drives it through Fleet's drive sessions with the blades off. The Device-Gateway's controller stays the safety authority.

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; the layer rules and the definition of done are in `docs/dev/service-template.md`.

## Purpose

- Owns proposals (one round of AI assist: the scene, the model's answer, the decision, the outcome), the step audit, short-lived images, the cost accounting of every model call and the users' own provider settings (keys encrypted at rest).
- Offers the generic **return to dock** as the lowest-risk proposal when the mower stands in a mapped area linked to the dock (same area, or mapped transfer paths) and no lift, tilt or other safety stop is active; when the model recommends nothing it is still offered as a labelled fallback. Approving it sends the normal `return_to_dock` through `fleet.commands.issue`; the mower drives home by itself.
- Runs approved maneuvers: a drive session on behalf of the approver (`fleet.drive.start`, `origin: vision`), `ManualDrive` frames at 5 Hz with `ttl_ms` 1000 and `blade_on = false`, a safety watch, `fleet.drive.stop`, a verification.
- Starts an analysis by itself when a mower enters a recoverable error and a user of the account opted in ([ADR 0035](../../docs/adr/0035-server-side-camera-frames-and-automatic-analysis.md)); the approval stays with a human.
- Gets photos of real cameras from the Device-Gateway (`gateway.mower.<id>.photo`), which takes them from the camera relay's frame tap ([ADR 0045](../../docs/adr/0045-camera-relay.md)). Still ships the deprecated capture worker `vision-capture` (`python -m vision.capture`, image built with `WITH_CAPTURE=true`), kept for one release as the gateway's fallback.
- Scales with the number of assist requests (rare, a few per mower and week) and keeps the newest state and map of every mower in memory.

## Model providers

`VisionProvider.analyse(images, context, task) -> Assessment` is the port. Implementations:

- **LiteLLM** (`adapters/providers/llm.py`): any vision-capable model family by model string. Documented families: Anthropic (`anthropic/claude-sonnet-5-5`, the default of the installation model, `anthropic/claude-opus-5-5`, `anthropic/claude-haiku-5-5`), OpenAI (`openai/gpt-5-mini`), Google Gemini (`gemini/gemini-2.5-flash`), Mistral (`mistral/pixtral-large-latest`), local Ollama (`ollama/llava`, `ollama/qwen2.5vl` with a base URL such as `http://ollama:11434`) and any OpenAI-compatible endpoint (`openai/<model>` with a base URL: vLLM, LM Studio, LocalAI, a proxy); also accepted: `ollama_chat/`, `openrouter/`, `azure/`. Structured output: the JSON schema as `response_format` when `litellm.supports_response_schema` says so (Claude gets native structured outputs, no forced tool use, no sampling parameters), otherwise the schema in the prompt and a tolerant parser; a 400 for the schema switches to prompt mode; an invalid answer gets one retry with the validation error; timeouts, connection errors, 429 and 5xx are retried (`MOWDECK_VISION_PROVIDER_RETRIES`). Tokens and LiteLLM's cost estimate (`completion_cost`, bundled price map, no request at startup) are recorded per call. `max_tokens` is 16000 (current Claude models think adaptively within it).
- **Fake** (`adapters/providers/fake.py`): `fake/recover` (recover: back 1 s, turn about 30 degrees, forward 1 s; find_dock: turn towards the dock and approach it, computed from the context), `fake/dock` (`return_to_dock` whenever the context's `dock_route` allows it, else like `fake/recover`), `fake/decline` (recommends nothing, like a cautious model: the dock fallback steps in when it applies), `fake/people` (risk flag), `fake/low_confidence`, `fake/empty`, `fake/fail`, `fake/invalid`.

## What the model gets

- **Images**: the camera frames (a stereo pair left first) or the gateway's photo, then a **top-down map rendering** (`domain/mapimage.py`, Pillow, 512 x 512 PNG, about 6 KB): heading up (the mower's forward direction is the top edge), the mower a magenta triangle in the centre, the dock a blue square (an arrow on the edge with the distance when it is further away), areas green with their names, transfer paths orange, no-go zones and obstacles red, the recent track cyan, a scale bar and a north arrow. It comes on top of `MOWDECK_VISION_MAX_IMAGES` and never pushes a camera frame out; it is stored and listed with the proposal (`frames[].source = map`). `MOWDECK_VISION_MAP_IMAGE=false` switches it off.
- **Scene context** (JSON, mower frame: `forward`/`left` metres): the mower state (`sensor_stop` for lift, tilt and safety stops; `drive_recoverable` only with activity `error`), the map within `MOWDECK_VISION_CONTEXT_RADIUS_M` (areas, transfer paths, no-go zones, obstacles, the dock with distance and bearing), `dock_route` (mower area, dock area, connected over mapped paths, the route, distance, `return_to_dock_applicable` with the reason; `domain/dock.py`), `recent_track` (the last positions with an RTK fix), recent commands and earlier rounds.
- **Answer**: `action` `return_to_dock` (preferred when applicable), `drive` (the steps) or `none`. `return_to_dock` is only proposed when the route check agrees; when the model answers `none` (or anything that is rejected) without a risk flag and the route check allows it, the service offers `return_to_dock` with `fallback: true` and the model's reasons in `detail`. The route check runs again on the newest state and map before the command is sent; a lift in between aborts it.

Which provider a request uses: the requester's own enabled provider (plan feature `ai_recover`), else the installation model (`MOWDECK_VISION_DEFAULT_*`, when enabled and the plan has `ai_recover_hosted`), else `409 vision-provider-missing`. Keys of users are write-only, AES-GCM encrypted with `MOWDECK_VISION_SECRETS_KEY` (user and account as associated data) and decrypted only in memory for one call. Images and keys are never logged.

## Interfaces

All routes are mounted under `/api/v1` (OpenAPI at `/api/v1/openapi.json`). Traefik routes `/api/v1/vision` to this service.

| Method | Path | Who | Description |
| --- | --- | --- | --- |
| GET | `/vision/status` | any principal | Hosted default (model string only), whether keys can be stored, the base URL policy |
| GET | `/vision/providers` | any principal | Model families with examples and key variables; whether the installation model may be used |
| GET, PUT, DELETE | `/vision/me/provider` | the caller | Own provider (key write-only: omitted keeps it, empty deletes it) and the effective provider |
| POST | `/vision/me/provider/test` | the caller | A minimal vision call with a tiny generated image: model, latency, tokens, cost (hourly limit) |
| GET, PUT | `/vision/me/preferences` | the caller | Own preferences (a field left out keeps its value): `stereo_frames` (default true) and `auto_analysis` (opt-in to automatic analyses on errors, default off; enabling needs `ai_recover`), with the installation's `auto_analysis_available`, `auto_cooldown_minutes`, `auto_daily_per_mower` |
| POST | `/vision/mowers/{id}/recover` | `can_drive` | Start AI assist (`task` `recover` or `find_dock`, base64 snapshots, each optionally with `camera`, `side` and `captured_at`); 202 with the proposal |
| POST | `/vision/mowers/{id}/recover/upload` | `can_drive` | The same with `multipart/form-data` (`task`, `images` files) |
| GET | `/vision/mowers/{id}/proposals` | `can_view` | Proposals of the mower, newest first |
| GET | `/vision/proposals/{id}` | `can_view` | One proposal with its steps; poll while `final` is false |
| GET | `/vision/proposals/{id}/images/{index}` | `can_view` | An image the model saw (until it is purged) |
| POST | `/vision/proposals/{id}/approve` | `can_drive` | Approve: drive the steps on the caller's behalf; one approval, refused after `expires_at` |
| POST | `/vision/proposals/{id}/reject` | `can_drive` | Reject a waiting proposal or cancel a running execution |
| POST | `/vision/proposals/{id}/retry` | `can_drive` | Another round after `not_recovered`, `aborted` or `rejected_by_ai` (up to `MOWDECK_VISION_MAX_ROUNDS`) |
| GET | `/healthz`, `/readyz`, `/metrics`, `/.well-known/mowdeck-module` | none | Operational endpoints of the kit |

Problems: `feature-not-in-plan` (403), `vision-provider-missing`, `vision-proposal-expired`, `vision-secrets-unavailable`, `conflict` (409), `vision-quota` (429), `vision-provider-failed` (502), `validation-failed` (422).

Proposals carry `trigger` (`manual`, or `error` for an automatic analysis) and `trigger_error_code`.

Proposal states: `analysing`, then `proposed` (waits for approval until `expires_at`), `rejected_by_ai` (any risk flag, confidence below `MOWDECK_VISION_MIN_CONFIDENCE`, no step) or `failed`; after approval `executing`, `verifying`, then `recovered`, `not_recovered` or `aborted`; or `rejected`, `expired`. At most one open proposal per mower.

## Events and subjects

| Direction | Subject | Contract | Notes |
| --- | --- | --- | --- |
| publishes | `event.vision.proposal.created` | `VisionProposalCreated` (with `trigger`, `error_code`) | Notifications turns it into `vision_proposal`, or `vision_auto_proposal` for an automatic one, for users with `can_drive` |
| publishes | `command.mower.<id>` | `CommandEnvelope` (`ManualDrive`) | frames of an approved maneuver, `correlation_id` `drive:<session>` |
| requests | `fleet.drive.start`, `fleet.drive.stop` | `DriveStartRequest` (`origin: vision`), `DriveStopRequest` (`completed`, `aborted`) | on behalf of the approver |
| requests | `fleet.commands.issue` | `IssueCommandRequest` (`origin: vision`) | `return_to_dock` after a successful Find Dock; `set_light` on and off around a dark photo (on the requester's behalf) |
| requests | `gateway.mower.<id>.photo` | `PhotoRequest`, `PhotoReply` | a still image when the app sent none |
| subscribes (every instance) | `telemetry.mower.*`, `device.mower.>` (newest per subject), `command.mower.*`, `command.result.mower.*` | | the view: states, maps, capabilities, recent commands, emergency stops, refused frames |
| subscribes (durable `vision-identity`) | `event.identity.>` | `UserChanged` | disabled or deleted users lose their provider settings and preferences |
| subscribes (durable queue `vision-auto`) | `telemetry.mower.*` | `MowerState` | automatic analysis on errors (not subscribed with `VISION_AUTO_ENABLED=false`) |

Dark photos (users mow at night): when the service takes the photo itself (the request carried no image), it computes the photo's mean luminance (ITU-R 601 luma on a downscaled greyscale, 0 to 255, Pillow through `mowdeck-control[photo]`, `domain/frames.py`). Below `MOWDECK_VISION_DARK_FRAME_THRESHOLD` (default 40), and only when the mower's capabilities have `light` and its state reports `light_on = false`, it issues `set_light` on through `fleet.commands.issue` on the requester's behalf (`origin: vision`; Fleet checks `can_control` and the plan features `control` and `camera`), waits until the state reports `light_on = true` (polled every 0.2 s, at most `MOWDECK_VISION_LIGHT_WAIT_SECONDS`, default 10 s; after that it goes on anyway), takes the photo again and analyses that one; the proposal records `light_used = true` (column added in migration 0003, `ProposalOut.light_used`; the web app shows "Taken with the camera light on"; on Mammotion mowers the light is the camera's headlamp). After the analysis, whatever its outcome, it issues `set_light` off, because the light was off before. A refused or unanswered `set_light`, or no second photo, never stops the analysis: it goes on with the dark photo (`light_used` stays false when the light could not be switched). A light that is already on, unknown (`light_on` null) or missing is left alone. This runs inside `POST /recover` before the 202 answer, so a dark request can take up to about 21 s longer (Fleet timeout 5 s, light wait 10 s, photo timeout 6 s). Images sent by the app (the camera panel's frame) are analysed as they are; on a real Mammotion mower the gateway's photo is a frame of the live camera through the camera relay ([ADR 0045](../../docs/adr/0045-camera-relay.md): a Vision lease for that one photo, the session of an open camera panel reused, the Vision limits), or through the deprecated capture worker (below) when the relay cannot serve the mower.

## Automatic analysis on errors

[ADR 0035](../../docs/adr/0035-server-side-camera-frames-and-automatic-analysis.md). Users opt in on the AI provider page ("Automatische KI-Analyse bei Fehlern", `PUT /vision/me/preferences {"auto_analysis": true}`, plan feature `ai_recover`). The durable consumer `vision-auto` hands every state to `AutoAnalysis` (`application/auto.py`):

1. A recoverable error (`domain/auto.py`): activity `error`, not `raw.lifted`, and the normalised code (lower case, a leading minus dropped) in `VISION_AUTO_ERROR_CODES`, or `drive_recoverable`. Default codes: stuck or trapped (`stuck`, 1201, 1421, 11112, 2000701), blocked by an obstacle (1206, 1214 channel or recharge route obstructed, 11113, 11121, 11122), outside the work area (1100, 1145, 1411, 1422), wheel slip (1032, 1218, 11132, 11134). Never lift or tilt stops, low battery, blade or motor errors.
2. Only entering an error counts: the shared watch `auto_watch` (one row per mower, swapped under a row lock) holds the current recoverable code; a replica skips the database for non-error states while it believes the mower is fine (refreshed every 60 s).
3. In a background task: states older than `VISION_AUTO_MAX_STATE_AGE_SECONDS` are ignored; no analysis when any proposal of the mower was created within `VISION_AUTO_COOLDOWN_MINUTES` or one is open; at most `VISION_AUTO_DAILY_PER_MOWER` automatic analyses per mower and UTC day; the plan needs `ai_recover`.
4. The opted-in users, longest opted in first: the first with `can_drive` on the mower (OpenFGA), a usable provider (own or the installation's) and daily quota left is the requester. The round is a normal one with the gateway's photo (none: no analysis) and the dark-frame light, recorded with `trigger = error`. Nothing is executed without an approval.

Skips are logged with a reason (`disabled`, `stale`, `cooldown`, `open_proposal`, `daily_budget`, `not_in_plan`, `no_subscriber`, `no_photo`).

## Capture worker (deprecated)

**Deprecated by [ADR 0045](../../docs/adr/0045-camera-relay.md)** (2026-10-18): the Device-Gateway takes Vision's photos from the camera relay's frame tap (`MOWDECK_RELAY_URL`, `MOWDECK_RELAY_FRAMES_TOKEN`) and asks this worker only when the relay is not configured, not reachable or has no driver for the mower's camera. It stays one release (Compose profile `capture`, Helm `services.visionCapture`) and is then removed. To drop it on a running installation once a photo of the mower came through the relay (the relay's log has `vision frame set served`, the gateway's `camera frame captured` with `source=RelaySnapshotter`): remove `capture` from `COMPOSE_PROFILES`, empty `GATEWAY_CAPTURE_URL` and `CAPTURE_TOKEN`, `docker compose --profile capture rm -sf vision-capture` and `docker compose up -d device-gateway` ([operations guide](../../docs/ops/compose.md#photos-of-a-real-camera-and-automatic-analyses)).

`python -m vision.capture` (`vision/capture/`), a separate process of the image built with `--build-arg WITH_CAPTURE=true` (the extra `capture`: Playwright 1.63.0, Chromium headless shell, the Agora Web SDK 4.24.8 checked against its npm sha512 at `/opt/agora`; about 0.5 GB more than the normal image). `POST /api/v1/capture` with a `CameraSnapshotRequest` answers one JPEG: a fresh browser context joins the Agora channel with the session's token, uid and encryption, waits for the requested camera, subscribes, waits for the first decoded frame (plus `CAPTURE_SETTLE_MS`), draws it into a canvas scaled to `max_width`, leaves. Limits: `CAPTURE_MAX_CONCURRENCY` captures at once (503 after `CAPTURE_QUEUE_SECONDS`), frame wait capped at `CAPTURE_MAX_TIMEOUT_SECONDS` (504), 502 with a short reason (`no-publisher`, `no-frame`, `agora:<code>`, `browser-error`), the JPEG quality lowered while above `max_bytes`. `Authorization: Bearer <MOWDECK_CAPTURE_TOKEN>` when set. Tokens and images are never logged. `/healthz`, `/readyz`, `/metrics` from the kit.

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_CAPTURE_TOKEN` | empty | Shared secret of the Device-Gateway (empty: no check) |
| `MOWDECK_CAPTURE_BACKEND` | `playwright` | `fake`: a generated picture without a browser (development) |
| `MOWDECK_CAPTURE_MAX_CONCURRENCY` | `2` | Captures at the same time |
| `MOWDECK_CAPTURE_QUEUE_SECONDS` | `10` | Wait for a free slot |
| `MOWDECK_CAPTURE_MAX_TIMEOUT_SECONDS` | `30` | Upper bound of a request's frame timeout |
| `MOWDECK_CAPTURE_JPEG_QUALITY` | `0.8` | JPEG quality |
| `MOWDECK_CAPTURE_SETTLE_MS` | `400` | Wait after the first frame for a complete key frame |
| `MOWDECK_CAPTURE_AGORA_SDK` | `/opt/agora/AgoraRTC_N-production.js` | The SDK file, or an `https://` URL |
| `MOWDECK_CAPTURE_BROWSER_CHANNEL` | empty | Playwright channel; `chrome` needs Google Chrome in the image (H.264) |
| `MOWDECK_CAPTURE_AGORA_PROFILE` | `rtc` | Channel profile of the join |

Stereo frames (the LUBA 2 AWD X has a front left and a front right camera): the camera panel can show both side by side, and then the web app sends both frames of the same moment, each `ImageIn` with `camera` (the session's name, e.g. `Front left`), `side` (`left`, `right`) and `captured_at` (aware timestamp). Two uploaded frames with one `left` and one `right` from two cameras, captured at most `MOWDECK_VISION_STEREO_MAX_SKEW_MS` apart (default 500; the browser aims at 200 ms; frames without times are accepted as sent), are a stereo pair (`domain/frames.py` `stereo_pair`). With the requester's preference `stereo_frames` on (`GET`/`PUT /me/preferences`, table `user_preferences`, default true when never saved) the model gets both, left first: the context says `stereo: true`, the user message carries the hint "Images 1 and 2 are the left and right front camera of the same moment, side by side (left first). Use the parallax between them for a rough distance estimate to obstacles; the camera spacing is not calibrated, so do not invent a distance when unsure." and every image follows its caption (`Image 1: Front left camera (left) captured 12:00:00.120 UTC`); the system prompt has the same rule. With the preference off only the left frame is analysed. A pair too far apart goes on as two plain images. The proposal records `stereo` and `frames` (index, source, camera, side, capture time per image, in the order the model saw them; `proposals.stereo`, `proposals.frames`, migration 0004); both images are stored and served at `images/0` and `images/1`. Cost: the recorded tokens and LiteLLM's estimate cover both images (two images cost about twice the image share of one); the cost row's detail says `images=2 stereo=true`. Light handling is unchanged: it only runs when the service takes its own photo (no images in the request), so a stereo request never switches the light, and `light_used` keeps its meaning. Single-frame requests (JSON without the new fields, or multipart uploads, which carry no camera metadata) work as before.

Both front cameras through the camera relay ([ADR 0045](../../docs/adr/0045-camera-relay.md)): when the service takes its own photo (no images in the request, and every automatic analysis) it asks the gateway for two frames of the same moment (`PhotoRequest.max_frames` 2). For a mower whose photos come from the relay the gateway answers with the relay's frame set: the left and the right front camera captured closest together (the relay aims at 200 ms), each with `camera`, `side` and `captured_at` (source `mower`). Two `mower` frames marked `left` and `right` within `MOWDECK_VISION_STEREO_MAX_SKEW_MS` are a stereo pair exactly like two uploads (never a mower frame with an upload), so the proposal records `stereo: true` and the model sees both, left first, with the hint; the camera the app shows plays no part. A set of one frame (a silent camera, a mower with one camera, an adapter with an own photo, an older gateway) is a plain photo. The light: when any frame of the set is too dark, the light goes on and the whole set is taken again. Whenever the model gets more than one camera image (the map rendering does not count) the user message adds the safety rule "Check every image on its own for risks. A person, child or animal, a drop-off, water or a road seen in any one image counts, even when another image does not show it [...] Never let one image overrule a risk seen in another.", and any risk flag still means nothing is proposed.

Abort conditions while executing: an emergency stop by anyone, a sensor stop (an error that is not `drive_recoverable`, or the lift flag), a new error, another activity (mowing, returning, docked), a refused frame, no state for `MOWDECK_VISION_STATE_TIMEOUT_SECONDS`, leaving the mapped areas plus `MOWDECK_VISION_GEOFENCE_MARGIN_M` (or the dock surroundings) or entering a no-go zone, a cancellation. No frame at all after an emergency or sensor stop.

## Settings

The kit settings (`MOWDECK_DATABASE_URL`, `MOWDECK_NATS_URL`, ...), OpenFGA (`MOWDECK_OPENFGA_*`), token verification (`MOWDECK_AUTH_JWKS_URL`) and the plan checks (`MOWDECK_ENTITLEMENTS_*`) apply. Secrets may be given as `MOWDECK_<NAME>_FILE`.

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_VISION_DEFAULT_ENABLED` | `false` | Offer the installation's own vision model to users without a provider of their own (plan feature `ai_recover_hosted`). Off by default: every user brings a provider. |
| `MOWDECK_VISION_DEFAULT_MODEL` | `anthropic/claude-sonnet-5-5` | LiteLLM model string of the installation default, e.g. `anthropic/claude-sonnet-5-5`, `openai/gpt-5-mini`, `gemini/gemini-2.5-flash`, `ollama/llava` or `fake/recover` (the rule based fake, for development and demos). |
| `MOWDECK_VISION_DEFAULT_API_BASE` | empty | Base URL of the installation default (Ollama, an OpenAI-compatible endpoint, a proxy). |
| `MOWDECK_VISION_DEFAULT_API_KEY` | empty | Key of the installation default. Empty: LiteLLM reads the family's usual variable. |
| `MOWDECK_VISION_SECRETS_KEY` | empty | 256-bit key (base64 or hex) that encrypts the users' API keys at rest. Without it users can only configure providers without a key (Ollama, open endpoints). |
| `MOWDECK_VISION_ALLOW_FAKE_PROVIDER` | `false` | Development only: users may pick the fake provider (`fake/...`), e.g. in the e2e stack. |
| `MOWDECK_VISION_USER_API_BASE_POLICY` | `any` | Which base URLs users may set: `any` (a home installation with Ollama on the LAN), `public` (no private, loopback or single-label hosts: hosted installations), `none`. |
| `MOWDECK_VISION_PROVIDER_TIMEOUT_SECONDS` | `90.0` | Timeout of one model call. |
| `MOWDECK_VISION_PROVIDER_RETRIES` | `2` | Retries of a model call after a timeout, a connection error, 429 or 5xx. |
| `MOWDECK_VISION_PROVIDER_TESTS_PER_HOUR` | `10` | Provider tests per user and hour. |
| `MOWDECK_VISION_PROPOSAL_TTL_SECONDS` | `300` | How long a proposal waits for approval; images are deleted after this time too. |
| `MOWDECK_VISION_MAX_ROUNDS` | `3` | Rounds (analysis, approval, execution) per assist session. |
| `MOWDECK_VISION_MIN_CONFIDENCE` | `0.6` | Answers below this confidence are `rejected_by_ai`. |
| `MOWDECK_VISION_USER_DAILY_REQUESTS` | `20` | Analyses per user and UTC day (own and hosted providers). |
| `MOWDECK_VISION_HOSTED_ACCOUNT_DAILY_REQUESTS` | `50` | Analyses with the installation default per account and UTC day. |
| `MOWDECK_VISION_KEEP_IMAGES_DAYS` | `0` | Keep the images of a proposal this many days longer than its TTL (0: delete with it). |
| `MOWDECK_VISION_MAX_IMAGES` | `4` | Camera or uploaded images per request (the map image comes on top). |
| `MOWDECK_VISION_MAP_IMAGE` | `true` | Send the top-down map rendering as one extra image after the frames. |
| `MOWDECK_VISION_MAX_IMAGE_BYTES` | `2097152` | Size of one uploaded image. |
| `MOWDECK_VISION_CONTEXT_RADIUS_M` | `8.0` | Radius of the map excerpt sent to the model. |
| `MOWDECK_VISION_FRAME_RATE_HZ` | `5.0` | Drive frames per second while executing (each with `ttl_ms` 1000, blades off). |
| `MOWDECK_VISION_STATE_TIMEOUT_SECONDS` | `3.0` | Abort when no state arrived for this long. |
| `MOWDECK_VISION_START_STATE_MAX_AGE_SECONDS` | `30.0` | Execution starts only with a state at most this old (a resting mower reports every heartbeat, 10 s on the Device-Gateway). |
| `MOWDECK_VISION_GEOFENCE_MARGIN_M` | `2.0` | Margin around the mapped areas automated driving may use. |
| `MOWDECK_VISION_SETTLE_SECONDS` | `1.5` | Wait after the last step before the outcome is checked. |
| `MOWDECK_VISION_MIN_MOVE_M` | `0.2` | Displacement that counts as "moved as planned". |
| `MOWDECK_VISION_LINEAR_MPS_AT_1` | `0.5` | Typical speed at `linear = 1` (told to the model). |
| `MOWDECK_VISION_TURN_DEG_PER_S_AT_1` | `57.0` | Typical turn rate at `angular = 1` (told to the model). |
| `MOWDECK_VISION_FLEET_TIMEOUT_SECONDS` | `5.0` | Timeout of `fleet.drive.*` and `fleet.commands.issue`. |
| `MOWDECK_VISION_PHOTO_TIMEOUT_SECONDS` | `35.0` | Timeout of a photo request to the Device-Gateway (a camera frame through the camera relay takes 2 to 15 s when the relay has to start the stream; keep it above the gateway's `MOWDECK_PHOTO_TIMEOUT_SECONDS`). |
| `MOWDECK_VISION_DARK_FRAME_THRESHOLD` | `40.0` | Mean luminance (0 to 255) below which the service's own photo counts as too dark: the light goes on for a second photo and off after the analysis. 0 disables it. |
| `MOWDECK_VISION_LIGHT_WAIT_SECONDS` | `10.0` | How long to wait for the mower to report the light on before the second photo. |
| `MOWDECK_VISION_STEREO_MAX_SKEW_MS` | `500.0` | Two camera frames (`left` and `right`) captured further apart than this are analysed as separate images, not as a stereo pair. |
| `MOWDECK_VISION_AUTO_ENABLED` | `true` | Allow automatic analyses for users who opted in; false switches them off for the installation (the opt-ins stay stored). |
| `MOWDECK_VISION_AUTO_COOLDOWN_MINUTES` | `10` | No automatic analysis for a mower within this time after the previous analysis (manual ones count). |
| `MOWDECK_VISION_AUTO_DAILY_PER_MOWER` | `20` | Automatic analyses per mower and UTC day (0: none); the per-user cap applies on top. |
| `MOWDECK_VISION_AUTO_ERROR_CODES` | see above | Comma separated error codes that start an automatic analysis (besides `drive_recoverable`). |
| `MOWDECK_VISION_AUTO_MAX_STATE_AGE_SECONDS` | `120.0` | Older states (a backlog after a restart) start nothing. |
| `MOWDECK_VISION_SWEEP_SECONDS` | `30.0` | Interval of the sweep (expiry, interrupted work, image purge). |

## Data model

`proposals` (one row per round, with `light_used`, `stereo`, `frames`, `trigger` and `trigger_error_code`, `action` (`drive` or `return_to_dock`) and `fallback` from migration 0006; a partial unique index allows one open proposal per mower), `proposal_steps` (per step: timestamps, frames, outcome), `proposal_images` (deleted after `delete_after`), `provider_requests` (cost accounting, daily caps), `user_providers` (model, base URL, AES-GCM ciphertexts of the key and the extra headers, last test), `user_preferences` (per user and account: `stereo_frames` from migration 0004, `auto_analysis` and the principal kind it acts as from migration 0005; deleted with the user), `auto_watch` (the current recoverable error per mower). Migrations: `python -m vision.adapters.db.migrate` (Compose `vision-migrate`, Helm a Job).

## Layers

| Layer | Contents |
| --- | --- |
| `domain` | models and the answer schema, families and base URL policy, scene context (mower frame), rules (evaluation, start and abort rules, geofence, verification) |
| `application` | `assist.py` (requests, decisions, rounds, sweeps), `executor.py` (drive session, frames, safety watch, verification, return to dock), `providers.py` (settings, resolution, test), `ports.py` |
| `adapters` | `api` (routes, schemas, problem mapping), `bus` (view, Fleet, photos, publisher, subscriptions), `db` (tables, store, migrations), `providers` (LiteLLM, fake, prompt and schema), `crypto.py`, `memory.py` |

## Local start

```bash
uv run pytest services/vision
docker compose -f infra/compose/docker-compose.yml up -d --build --wait vision
```

For an offline demo with the simulator set `VISION_DEFAULT_ENABLED=true` and `VISION_DEFAULT_MODEL=fake/recover` in `infra/compose/.env` and register a simulated mower with the connection option `{"stuck_after_seconds": 20}` (development only); add `"night": true` for dark photos, so the light handling runs (close the camera panel, or the app sends its own frame). See [Compose operations, AI assist](../../docs/ops/compose.md#ai-assist-vision) for keys, local models and costs.

## Tests

`uv run pytest services/vision` (coverage about 94 %). `test_vision_auto.py` drives the automatic analysis end to end with a simulated mower that gets stuck and checks every limit; `test_vision_capture.py` the worker API with fake grabbers and, with `MOWDECK_TEST_CAPTURE_BROWSER=1` and Playwright installed (`uv run --with playwright==1.63.0`, `python -m playwright install chromium-headless-shell`), the real page script in headless Chromium against a stub of the Agora SDK. The flow tests run the real app against a simulated mower behind a real `MowerController` on the in-memory bus; `test_vision_llm.py` replays recorded Anthropic, OpenAI-compatible and Ollama responses through the real LiteLLM code path from a local HTTP server (no network) and checks the request shape. PostgreSQL (`MOWDECK_TEST_DATABASE_URL`) and NATS (`MOWDECK_TEST_NATS_URL`) tests run when the variables are set.
