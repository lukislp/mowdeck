# Chat control (assistant add-on)

Chat control lets users type what a mower should do ("mäh morgen früh nur hinten", "send it to the station"). The optional service `assistant` turns the request into a work order of validated actions; the user confirms it in the web app, which then sends the actions through the normal command and schedule endpoints. Design and safety rules: [ADR 0075](../adr/0075-chat-control-assistant-add-on.md). For users: [guide](../guides/chat-control.md).

It is off unless you enable it. Without it nothing changes and no chat box appears.

## What it needs

- **No secret, no database, no NATS user.** The assistant only calls MowDeck's HTTP API (Identity, Fleet, Telemetry, Vision) with the token of the user who asked.
- **An AI provider** for the default interpreter `provider`: each user's own provider (Settings, AI provider) or the installation's model (`VISION_DEFAULT_*`, plan feature `ai_recover_hosted`), the same as AI assist ([ADR 0033](../adr/0033-vision-assisted-recovery-with-approval.md)). Users without one see a link to the provider settings. Vision limits the requests per user and hour (`VISION_STRUCTURED_PER_HOUR`, default 30) and records them in its cost accounting with kind `structured`.
- Or the interpreter **`phrases`**: a built-in phrase parser for a few German and English requests, without any AI. Demo mode always uses it.

## Docker Compose

In `infra/compose/.env`:

```bash
COMPOSE_PROFILES=assistant            # comma separated with other profiles, e.g. relay,assistant
IDENTITY_EXTRA_MODULE_URLS=http://assistant:8000
ASSISTANT_INTERPRETER=provider        # or phrases
ASSISTANT_REQUESTS_PER_MINUTE=20
ASSISTANT_AI_TIMEOUT_SECONDS=60
```

Then `docker compose up -d --wait`. Identity lists the assistant's UI area within a minute (it polls missing module URLs every 30 s); users reload the app once to see the chat box on the mower page.

To remove it, take `assistant` out of `COMPOSE_PROFILES` and its URL out of `IDENTITY_EXTRA_MODULE_URLS`, then `docker compose up -d --wait --remove-orphans`.

## Helm

```yaml
services:
  assistant:
    enabled: true
config:
  ASSISTANT_INTERPRETER: provider
```

The chart adds the assistant to Identity's module list and to the Ingress (`/api/v1/assistant`) only when it is enabled.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `ASSISTANT_INTERPRETER` | `provider` | `provider`: the user's AI provider through Vision; `phrases`: the phrase parser without AI. Demo mode forces `phrases`. |
| `ASSISTANT_REQUESTS_PER_MINUTE` | `20` | Proposals per user and minute (per replica). |
| `ASSISTANT_AI_TIMEOUT_SECONDS` | `60` | Timeout of one structured answer from the AI provider. |
| `VISION_STRUCTURED_PER_HOUR` | `30` | Vision: structured answers per user and hour (the AI cost bound). |

The service-internal URLs (`MOWDECK_ASSISTANT_IDENTITY_URL`, `..._FLEET_URL`, `..._TELEMETRY_URL`, `..._VISION_URL`) default to the Compose service names; the chart sets its own.

## Demo

A public demo ([ADR 0068](../adr/0068-public-demo-mode.md)) takes no Compose profiles, so the demo stack does not run the assistant. If you run it next to a demo yourself, it uses the phrase parser regardless of `ASSISTANT_INTERPRETER` and calls no external AI.
