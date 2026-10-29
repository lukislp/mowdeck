# ADR 0067: Error code lexicon, provider independent

## Status

Accepted (2026-10-29)

## Context

A mower reports its faults as vendor codes: Mammotion sends numbers such as `-1201`, and the adapters for Husqvarna, Navimow and Worx that are being built will bring codes of their own. MowDeck showed them as they came (`1201 The robot is stuck`): the code, the vendor's terse English text and, since the error category, one neutral word. Users want to know what happened, how bad it is, why and what to do, in German or English, in the app, in the push text and through the API (the Home Assistant integration, the widgets).

Forces:

- **The core stays vendor neutral.** Mower control is extended through entry points (`mowdeck.connections`, [ADR 0012](0012-mower-control-interface-and-safety-rules.md), [ADR 0019](0019-mammotion-adapter-transports.md)); the core services and the web app must not hold a vendor's code table. The texts belong to the adapter package that knows the vendor.
- **Licences and images.** Only the Device-Gateway installs adapter packages; the Mammotion adapter is GPL-3.0 and imports PyMammotion ([ADR 0011](0011-gpl-package-for-pymammotion-adapter.md)). Telemetry and Notifications must not install it to get at its texts.
- **Data at hand.** PyMammotion 0.10.9 (pinned) bundles the vendor's own export of the error table: 664 rows, English for all, German for about two thirds (partly machine translated: 2801 "Lift sensor triggered" is "Hub auslösen"), a "solution" text for most, and 19 codes PyMammotion read out of the firmware with its own English words.
- **The theft alarm** ([ADR 0027](0027-health-history-and-theft-alarm.md)) recognises a lift by `MOWDECK_NOTIFICATIONS_LIFT_ERROR_CODES`, a list of Mammotion codes in a core service. A second vendor would need its codes in that list.
- **Graceful.** A code no lexicon knows, a vendor without a lexicon, a gateway that does not answer: the user still sees something useful, never an error page.

## Decision

### The neutral contract (`mowdeck-contracts`)

`mowdeck_contracts.v1.error_lexicon` defines the data, nothing vendor specific:

- `ErrorLexicon`: `vendor` (lower-case key, the entry point name), `vendor_name`, `connection_kinds` (the kinds whose mowers report its codes), `source` (provenance) and the `entries`.
- `ErrorLexiconEntry`: `code` and `aliases` (other spellings such as `-1201`), `category` (`MowerErrorCategory`), `severity` (`info`, `warning`, `error`, `critical`), `user_action`, `texts` per locale (`ErrorText`: `title`, `description`, `cause`, `steps`; `en` is required, `de` is MowDeck's second language), `links` (https only) and `verified` (false when the meaning could not be checked against a vendor source).
- `ErrorExplanation`: one code explained in one locale, with `known`, the vendor, the texts after the locale fallback, severity, category, `user_action`, links and `verified`.
- `explain_code` and `unknown_explanation`: the lookup with the fallbacks. A missing locale falls back to English; an unknown code (or no lexicon) gives "Unknown error 1234 from Mammotion" ("Unbekannter Fehler 1234 von Mammotion") with MowDeck's generic advice for the state's category (`generic_text`, written per category, no vendor words).

`MowerErrorCategory` gains `communication`, `docking` and `firmware`, so the categories cover what a lexicon needs (stuck, obstacle, outside, wheel slip, lifted or tilted, collision, battery, blade or motor, rain, positioning, connection, charging station, firmware, other). The new values are additive; the Mammotion mapping now sorts codes that were `other` into them.

### Lexicons register through an entry point (`mowdeck-control`)

`mowdeck_control.error_lexicons` loads the entry point group **`mowdeck.error_lexicons`**: the name is the vendor key, the object an `ErrorLexicon` (plain data), an `ErrorLexiconProvider` (the data plus an optional `normalise(raw) -> code` function) or a callable without arguments that returns either (built at the first lookup). The registry picks a lexicon by vendor, or by the mower's connection kind (`MowerState.connection`) through `connection_kinds`. A broken or misnamed entry point is logged and skipped, like a broken connection adapter. Adapters can also call `provider.category_of(code)` to fill `MowerState.error_category`.

```toml
[project.entry-points."mowdeck.error_lexicons"]
mammotion = "mowdeck_control_mammotion.lexicon:mammotion_lexicon"
```

- **Mammotion** (`mowdeck_control_mammotion.lexicon`): built from PyMammotion's bundled table, every code except "Request succeeded" (663 codes). The vendor's implication is the title, its solution is split into steps; German only where the table has a German title and German steps, so no entry mixes languages (475 codes have German). 26 codes MowDeck acts on (stuck, blocked, outside, wheel slip, lifted or tilted, battery, positioning) have curated English and German texts with description, cause and steps. Categories come from the same `errors.category_of` that fills `MowerState.error_category`, so lexicon and state never disagree. The 19 firmware-only codes are `verified: false`. `normalise` drops the sign.
- **Simulator** (`mowdeck_control.simulator_lexicon`, vendor `simulated`): its two codes `stuck` and `lifted`, so the whole path can be tested without a mower.

### Who serves it

The Device-Gateway is the only service with the adapters installed. It answers two core NATS requests, any instance (queue group `device-gateway`): `gateway.errors.explain` (`ErrorExplainRequest`: up to 20 codes, vendor or connection, locale, category) and `gateway.errors.lexicons`. It holds no table of its own.

Telemetry is the public API (it already serves the mower state to the web app, the Home Assistant integration and the widgets):

| Route | Permission | Answer |
| --- | --- | --- |
| `GET /api/v1/telemetry/mowers/{id}/error?locale=de` | `can_view` | `MowerErrorOut`: the current error explained (`error` null without one) |
| `GET /api/v1/telemetry/mowers/{id}/error-codes?code=..&code=..` | `can_view` | explanations with the mower's lexicon (the health page's error counts) |
| `GET /api/v1/telemetry/error-codes/{vendor}/{code}` | signed in | one code of a vendor |
| `GET /api/v1/telemetry/error-lexicons` | signed in | the installed lexicons (counts, unverified, locales) |

The locale comes from `locale`, else `Accept-Language`, else English. Telemetry caches answers for an hour; when the gateway does not answer it serves the generic explanation (not cached).

### The app and the push texts

The mower page shows the explanation as a card (severity and category, vendor and code, title, description, likely cause, the steps as a numbered ladder; a hazard stripe for critical errors; unknown and unverified entries are marked) and keeps the old one-line notice while it loads or when Telemetry cannot answer. The health page puts each counted code's title next to it. The push text of `mower_stuck` ("Fehler 1201. {error_detail}") carries the lexicon's title and first step in the recipient's language; without an answer it keeps the vendor's message as before. The web app only translates the frame (severity names, labels); the vendor words come from the lexicon.

### Categories for the theft alarm

Notifications counts a state as lifted when `raw.lifted` is true **or its `error_category` is in `MOWDECK_NOTIFICATIONS_LIFT_CATEGORIES` (default `lift_tilt`)**. Both signals are provider neutral: a vendor's adapter sets `error_category` from its lexicon and needs no codes in a core setting. `MOWDECK_NOTIFICATIONS_LIFT_ERROR_CODES` stays as an optional operator override (a code listed there counts too) and is **empty by default**; it used to list the simulator's `lifted` and Mammotion's lift and tilt codes 1004, 1030, 2502, 2801, 11131 and 11133. The Mammotion adapter reports exactly these codes with `error_category` `lift_tilt` and `raw.lifted` (its tests check it on the state as it travels over the bus), so the alarm fires for the same states as before.

The automatic AI analysis on errors ([ADR 0035](0035-server-side-camera-frames-and-automatic-analysis.md)) follows the same rule: it starts on `drive_recoverable`, which the adapters set for the recoverable categories (trapped, obstacle, outside, wheel slip); `MOWDECK_VISION_AUTO_ERROR_CODES` is an optional override, empty by default (it used to list Mammotion's recoverable codes, which the adapter marks `drive_recoverable`). The architecture test fails when a core service's settings default to an error code list again.

### Alternatives

- **Tables in the core or the web app** (a JSON per vendor in `apps/web`): rejected, it couples the core to every vendor and duplicates what the adapter knows.
- **Telemetry installs the adapter packages**: rejected, it would put PyMammotion (GPL) and every vendor SDK into a second image for a few texts.
- **Lexicons as snapshots on the `DEVICE` stream**, stored by Telemetry: no request per lookup, but the Mammotion lexicon in two languages is about half a megabyte per message and a mapping function cannot travel as data. Request/reply with a cache is simpler.
- **Explanations inside every `MowerState`**: rejected; the state would carry texts in every language with every heartbeat.

## Consequences

- A new vendor adds its lexicon in its own package and one entry point; core, contracts and the web app stay unchanged ([Adding an error lexicon for a vendor](../dev/error-lexicon.md)). An architecture test (`tools/repo-checks/tests/test_vendor_isolation.py`) fails when core code imports an adapter package or its vendor library, when a service other than the Device-Gateway depends on one, or when an adapter package registers connections without a lexicon. The existing exception, the linked cloud accounts of [ADR 0034](0034-linked-cloud-accounts-and-device-discovery.md), is listed in the test.
- Explanations depend on the Device-Gateway being up; without it the app shows the generic advice and the push the vendor's message.
- The Mammotion texts follow PyMammotion's table: upgrading PyMammotion (deliberately, ADR 0019) refreshes them; the curated codes are checked by the adapter's tests.
- `MowerErrorCategory` has three new values: clients with an exhaustive list (the Home Assistant integration's enum sensor) must accept them.
- The widgets endpoint (`GET /mowers/{id}/widget`) keeps `error_code` and `error_message` and adds `error_title`, `error_category` and `error_severity`, resolved through the lexicon in the requested locale, so the iOS widgets, the Live Activity and the watch show the plain-language title from their stored snapshot without a second request. The Live Activity's final `error` update carries `errorTitle` in each recipient's notification language. All new fields are optional; older app builds ignore them.
