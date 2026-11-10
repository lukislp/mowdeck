# ADR 0081: Explicit consent before camera pictures or text go to a cloud AI provider

## Status

Accepted (2026-11-10). Extends [ADR 0033](0033-vision-assisted-recovery-with-approval.md) (AI assist and the provider resolution), [ADR 0035](0035-server-side-camera-frames-and-automatic-analysis.md) (automatic analysis) and [ADR 0075](0075-chat-control-assistant-add-on.md) (chat control).

## Context

MowDeck sends user data to an AI model in three features:

1. **AI assist** ("recover", "find dock"): up to four camera pictures, which can show people, children, animals and the neighbours' property (the system prompt even asks the model to flag them), a map sketch with the names of the user's areas, and the scene context (position, activity, error code, signal).
2. **Automatic analysis on errors** (ADR 0035): the same, started by the service when a mower reports a recoverable error. Nobody acts at the moment the data is sent; the user opted in once, under a text that did not name the third party.
3. **The chat assistant** (ADR 0075): the typed text, the mower's name, the area names, the activity and the time, through Vision's structured answer endpoint.

When the model is a cloud model (OpenAI, Anthropic, Google, Mistral and so on, the user's own or the installation's), that is personal data going to a third party. Apple's App Review Guideline 5.1.2(i) asks for disclosure and explicit permission before personal data is shared with a third party, "including with third-party AI", and the GDPR asks for the same transparency. Until now the only hint was a note on the settings page that appeared when a user configured their own cloud provider. A user of the installation's model never saw it, and nothing asked before the first picture left.

The native iOS and Android apps render the same web UI from the server ([ADR 0036](0036-native-app-shells-with-capacitor.md)), so one change in the web app covers all clients.

Requirements:

- Consent is **explicit** (a button, not a pre-ticked box), **informed** (what, to whom, that it leaves the installation, that it is optional, how to withdraw) and **withdrawable**.
- It is **enforced on the server**, not only in the UI: a script, the assistant add-on or a stale client must not be able to bypass it.
- **One rule in one place.** No feature may be able to forget it.
- Nothing changes for local models, the demo and the provider test, which do not send the user's data to a third party.

## Decision

### Where data goes: `Destination`

The domain (`vision.domain.consent`, pure, no I/O) derives a `Destination` (kind, provider label, family, model) from the effective provider:

- `simulated`: the rule based fake model (`fake/...`: the public demo, development). Nothing leaves the process.
- `local`: the user's or the operator's own hardware.
- `cloud`: everything else, **including every model nobody knows**. Unknown means cloud.

Who decides where a request goes differs, so there are two classifications, and the domain exposes them as two functions that cannot be mixed up:

- **A user's own provider (`classify_own`)** is classified by a heuristic, because the person whose data it is chose the endpoint. `fake/...` is simulated. A family flagged `local` ([`domain/families.py`](https://github.com/lukislp/mowdeck/blob/main/services/vision/src/vision/domain/families.py): Ollama, and the "OpenAI-compatible endpoint") is local when its base URL points into a private network (loopback, LAN, a single-label host such as `http://ollama:11434`); Ollama without a base URL is local too (its default base is on the LAN). Because the prefix `openai/` is shared by OpenAI's cloud and by any OpenAI-compatible endpoint, `openai/<model>` is local only with a private base URL. A "local" family whose base URL is public (a hosted Ollama, a public proxy) is cloud.
- **The installation's model (`classify_installation`)** is never classified by guessing. The user did not choose it and cannot see where the operator's gateway forwards to: `openai/...` with `http://litellm:4000` may be a private address in front of a cloud provider. The only thing that makes it local is the operator's explicit setting **`MOWDECK_VISION_DEFAULT_LOCAL=true`** (`VISION_DEFAULT_LOCAL` in Compose and Helm, default `false`); without it the installation's model is cloud whatever its prefix or base URL. `fake/...` stays simulated either way.

The classification is exposed, never guessed by the client: `GET /vision/status` (`hosted_default_kind`), `GET /vision/providers` (`hosted_destination`) and `GET /vision/me/provider` (`destination`, the one in effect now).

### The rule: `Consent`

A `Consent` is the time a user agreed and the destination it was given for. A destination is identified by provider family, model and **endpoint**: the normalised host (and the port when it is not the scheme's default) of the effective base URL, or `default` for the provider's own endpoint; for the installation's model it is the host of the operator's configured base URL. The endpoint is part of the recipient: the same model string at another address is another third party. Two pure functions decide:

- `consent_required(destination)`: only `cloud` requires consent.
- `covers(consent, destination)`: yes when **provider family, model and endpoint all match**. A changed provider, model or endpoint needs a new consent, because the third party or the model it runs changed. The dialog names the host when it is not the provider's default endpoint.

Consent is keyed by user **and** account (a sub-user has their own; `_ids(principal)` takes both from the verified token, so a request can only read, grant or withdraw its own), stored in `user_preferences` (revision 0008: `consent_at`, `consent_kind`, `consent_provider`, `consent_family`, `consent_model`, `consent_endpoint`, all null without consent) behind the existing `Store` port. A user who withdraws loses it entirely.

### One enforcement point

`ConsentGate.check(principal, destination)` (`vision.application.consent`) raises `ConsentRequired` when consent is required and not covered. `ProviderService.resolve()` is the one place that decides which model a request uses (own provider, else the installation default, else `vision-provider-missing`); it calls the gate before it returns the configuration. Every path that sends user data to a model goes through it, so none can forget it:

| Path | Where it resolves | Refused before |
| --- | --- | --- |
| Manual AI assist, `recover` and `recover/upload` | `VisionService._start_round` | any picture is captured, any image analysed, any proposal created |
| Retry of a proposal | `VisionService._start_round` | the same |
| Automatic analysis | `VisionService.request_auto` | the photo is asked for |
| Chat assistant (structured answer) | `ProviderService.structured` | the question is sent, any usage accounted |

The **provider test** is exempt: it sends a synthetic image the service generated itself and never resolves. **Simulated and local destinations** never require consent, so the demo and local models fall out of the same rule, with no special case in the UI or the routes.

`ConsentRequired` is a domain error. The API adapter maps it to `403 urn:mowdeck:problem:vision-consent-required`; the destination travels in `errors` (`parameter: destination`, `code`: the kind, `detail`: provider and model), the way `feature-not-in-plan` carries its feature. The **assistant** service does not learn any rule: it recognises Vision's problem type in the 403 of the structured call and answers its own `403 assistant-consent-required`, which the web app treats the same way.

### Automatic analysis

Enabling it (`PUT /vision/me/preferences`, `auto_analysis: true`) needs a covering consent when the destination is cloud (`ConsentService.require_current`; nothing is saved otherwise). At sending time `AutoAnalysis.trigger` reaches the gate through `request_auto`, with the principal of the opted-in user whose provider and quota the analysis uses, so it is that user's consent that counts; an opted-in user without a covering consent is skipped, and the skip is recorded like the others (`Skip.NO_CONSENT`, logged with the user), never silent. Withdrawing a consent switches the user's automatic analysis off. The gate runs at the start of the request (`request_auto`, `_start_round`) and once more right before the model is asked (`VisionService._analyse`): a consent withdrawn while the photo is taken or the light is switched on ends the proposal as `failed` ("consent ... withdrawn") with nothing sent. The only remaining window is a request already on its way to the provider (milliseconds to the model's answer), which cannot be recalled.

### Existing users

Nobody has a consent record after the migration, so **everyone is asked on first use**. Users who already switched automatic analysis on are **not grandfathered**: their opt-in was given under a text that did not name the third party, so it pauses (`no_consent`) until they agree in the app. A what's-new entry says so, and the AI provider settings show the paused state.

### API

| Route | What |
| --- | --- |
| `GET /vision/me/consent` | `required`, the current `destination`, `granted_at`, `granted_for`, whether it `covers` the destination |
| `PUT /vision/me/consent` | Agree to the provider in effect now (replaces an earlier consent; records nothing for a destination that needs none) |
| `DELETE /vision/me/consent` | Withdraw; automatic analysis goes off |

### Web app

One client module (`apps/web/src/lib/consent/consent.svelte.ts`: the three calls, the dialog state and `guard(scope, run)`) and one dialog (`ConsentSheet.svelte`, mounted once in the root layout like the step-up sheet). `guard` asks first, runs the request that sends data, and when the service still answers with the consent problem (the provider changed meanwhile) asks again and repeats once. A refusal ("not now") returns a marker and the feature stays unused without an error. The features only wrap their request: `AssistStore.start` and `retry`, `ChatControl.ask`, and the AI provider settings (saving an enabled provider, switching automatic analysis on). The settings also show the state (provider, model, date, withdraw), for users of the installation's model as well. The texts are German and English and name what is sent: for AI assist and the automatic analysis the camera pictures (people and neighbouring property may be visible), a map sketch with the area names and the mower status; for the chat the typed text and the mower and area names.

Saving a provider asks for consent right after it is saved, because the classification of the new provider is the service's, not the form's; declining keeps it saved, and every feature asks again before it sends anything.

### Options considered

- **UI only (a checkbox in the settings, a dialog in the app):** rejected. A script or the assistant add-on could still send data, and a stale client could skip the dialog.
- **Check in each of the four callers:** rejected. A fifth caller would forget it. The provider resolution is the one place all of them share.
- **One consent for "AI" regardless of provider:** rejected. The third party is what the user agreed to; switching from a provider to another one, or to a different model, changes who gets the pictures.
- **Classify by the model string alone:** rejected for `openai/`: it is both OpenAI's cloud and every local OpenAI-compatible server.
- **Guess the installation's model as local from a private base URL or the `ollama/` prefix:** rejected. The user cannot check it and the operator's gateway may forward to a cloud provider; only the operator's explicit `VISION_DEFAULT_LOCAL` counts.
- **Bind the consent to family and model only:** rejected. The same model string at another endpoint is another recipient.
- **Grandfather automatic analysis opt-ins:** rejected, see above.

## Consequences

- Nothing leaves the installation to a cloud model without a recorded, specific agreement. The check is on the server, so every client (web, apps, API scripts, the assistant) is covered, and a user who agreed under one provider is asked again for another.
- Operators with a local installation model set `VISION_DEFAULT_LOCAL=true` to spare their users the dialog; forgetting it only costs one extra click per user (the safe side).
- A consent for a provider the user later replaces stays on record until withdrawn; it simply does not cover the new destination.
- Existing automatic analyses pause until the user agrees; the settings page says so. The cost is a quiet period after the upgrade for users who relied on it.
- The privacy policy template ([`apps/web/static/legal/privacy.md`](https://github.com/lukislp/mowdeck/blob/main/apps/web/static/legal/privacy.md)) gets an "AI features" section describing the data flow and the consent; operators fill in their provider.
- The classification of a user's *own* provider by base URL treats a private-network host as local: the user chose that endpoint and knows what is behind it. A public DNS name that resolves to a private address counts as cloud (the safe side). The installation's model gets no such guess.
- A user who points the same model string at another host is asked again, and the dialog shows the host.
