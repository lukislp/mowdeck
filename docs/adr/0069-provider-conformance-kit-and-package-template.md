# ADR 0069: Provider conformance kit and provider package template

## Status

Proposed (2026-10-30). Builds on ADR 0012 (connection interface and safety rules), ADR 0046 (settings as capabilities), ADR 0066 (cloud provider plugins, "no dead buttons") and ADR 0067 (error lexicons); changes none of their contracts.

## Context

MowDeck is a set of building blocks: a mower vendor plugs in as its own package (`libs/mowdeck-control-<vendor>`) through entry points, and MowDeck shows exactly what that vendor's mowers can do, gated by `MowerCapabilities`, capability-declared settings and provider-declared options ([ADR 0066](0066-cloud-provider-plugins-and-more-vendors.md)). Three adapters exist (Mammotion, Husqvarna, Worx). Each tests itself with its own fake vendor client, and each re-tests the shared contracts in its own words: the Husqvarna package checks that its spec imports without the SDK and that its descriptor has German texts, the Worx package checks its lexicon against its state mapping, `libs/mowdeck-control/tests/test_control_minimal_adapter.py` checks a hand-written minimal adapter. Nothing checks every adapter against the same list, so a contract one adapter forgets goes unnoticed: the Husqvarna adapter, for example, declares neither map areas nor start plans, so no client can offer it a start, although the ADR 0066 amendment names it as the case start plans were made for.

Starting a new vendor means copying an existing adapter and deleting what does not fit, which drags vendor details along and makes the licence an accident of the copied package (the Mammotion and Worx packages are GPL-3.0 because their SDKs are, [ADR 0005](0005-licensing.md), [ADR 0011](0011-gpl-package-for-pymammotion-adapter.md)).

Forces: the core must stay vendor neutral (no core code imports an adapter or names a vendor, `tools/repo-checks/tests/test_vendor_isolation.py`); a vendor with few features must plug in with little effort; checks may only enforce contracts that exist; no new heavy dependency.

## Decision

### The conformance kit: `libs/mowdeck-provider-testkit`

A new Apache-2.0 library `mowdeck-provider-testkit` (import `mowdeck_provider_testkit`) holds one vendor-neutral pytest suite. A provider package subclasses it in its tests and names itself and its fake vendor client:

```python
class TestAcmeConformance(ProviderConformance):
    subject = ProviderUnderTest(
        distribution="mowdeck-control-acme",
        fake_credentials={"email": "garden@fake.mowdeck.test", "password": "fake-cloud-password"},
        emitted_error_codes=lambda: (str(code) for code in SDK_ERROR_TABLE),
    )
```

The kit derives everything else from the installed distribution: its entry points (loaded through `mowdeck_control`'s own loaders, the ones the Device-Gateway uses, with a source that yields only this distribution's entry points) and its `pyproject.toml` (`[tool.mowdeck.adapter]`). For a cloud provider it builds the connection the way the gateway does: `ProviderSpec.create(ProviderContext(fake=True))`, `discover` with the fake credentials, `create_connection` for the first online mower with the device name, IoT id, provider id and credentials in the options, wrapped in `MowerController`. A package without a cloud provider (or with a special fake) passes a `connection_factory` instead. Every command goes through the controller, so the checks see what a user sees. The checks are plain functions in `mowdeck_provider_testkit.checks` that raise `AssertionError`; the pytest class only calls them, skips what does not apply (with the reason) and turns `known_violations` (test name to reason) into strict xfails, so a fixed violation fails until it is taken off the list.

Why a new library and not a module of an existing one: the kit depends on pytest and pytest-asyncio, which must not become runtime dependencies of `mowdeck-control` (installed in the gateway image) or `mowdeck-contracts`; a test-only package keeps them out of every image. Its name has no `mowdeck-control-` prefix, so the vendor isolation test treats it as core: it may not import a vendor package, name a vendor or load code dynamically, and it does none of that.

### What the kit checks, and from where

| Check | Source |
| --- | --- |
| Package named `mowdeck-control-<vendor>`, import package installed | ADR 0066 ("How to add a vendor", decision 6) |
| Registers `mowdeck.connections` or `mowdeck.cloud_providers`, ships `mowdeck.error_lexicons`, every entry point points into its own package | ADR 0066 decision 6, ADR 0067 |
| Every entry point loads and names what it registers consistently (a known `ConnectionKind`, `ProviderSpec.provider` equal to the entry point name, lexicon `vendor` equal to the entry point name) | ADR 0012, 0066, 0067 and the loaders in `mowdeck_control` |
| `vendor-modules` lists the SDK, `brand-names` is a list | ADR 0066 decision 6 |
| No import of another adapter package | ADR 0066 decision 6 |
| The provider spec imports with the vendor modules blocked | `mowdeck_control.providers` ("should be importable without the vendor SDK") |
| Valid provider id, descriptor names the same provider, on by default only when tested on hardware, descriptor survives JSON, every text in `en` and `de`, the fake credentials fit the fields | ADR 0066 decisions 1 and 2, the localized-texts amendment |
| The fake account lists a mower; a changed secret raises `CloudLoginRefused` whose message does not repeat it | ADR 0066 decision 1, `CloudLoginRefused` |
| `is_connected` only between `connect` and `disconnect`, both idempotent; `watch_state` yields the current state first and ends on `disconnect` | ADR 0012, `MowerConnection` |
| The connection's kind is in `connection_kinds`; every declared optional feature implements its protocol (settings, maintenance, undock, firmware, base station firmware, drive speed setting, camera, bridge scan) | `mowdeck_control.connection` ("declares X and implements Y") |
| States belong to the mower and fill no gateway-owned field (`heartbeat_at`, `cloud_provider`, `connected`); `drive_recoverable` only during an error and never for lift, tilt or collision; `error_category` only during an error and equal to the lexicon's category; only declared settings with valid values | ADR 0012, ADR 0046, ADR 0067, field descriptions of `MowerState` |
| Map areas or start plans exist, so a client can offer a start | ADR 0066 amendment (start plans) |
| Start (as a client sends it), pause, resume, emergency stop while mowing, return to dock, return ending the job: each executed or accepted | ADR 0012 interface, ADR 0066 ("a minimal adapter ... starts, pauses and docks") |
| Everything the capabilities do not offer is refused with a reason before the adapter is asked (manual drive, drive calibration, blade height outside the range, speed, light, keeping the job, firmware, base station firmware, drive speed, each undeclared maintenance action, an undeclared setting, an unknown area, an undeclared pattern) | ADR 0066 principle, ADR 0012 |
| Everything they offer is taken: blade height at both ends of the range, speed, light on and off, every start plan, `ReturnToDock(end_job=False)` with `dock_keeps_job`, a native pattern on a map area; an unknown start plan is refused | ADR 0066 principle and amendment, ADR 0012 |
| Every declared setting takes a valid value; `executed` means the state shows it | ADR 0046 |
| Without `manual_drive` a drive session is refused with `CommandRejected`; with it, a session opens, takes a zero frame, and `stop` and `close` are idempotent | ADR 0012, `DriveSession` |
| The emergency stop is taken at rest, returns within 5 s while a start is still in flight, and still reaches the adapter on a mower the vendor reports offline | ADR 0012, `MowerConnection` rules, `docs/ops/vendor-lib-updates.md` |
| The lexicon survives JSON, has English titles, each code finds its own entry, it is found by the package's provider ids and connection kinds, and it explains every code the adapter can report | ADR 0067 |

Assumptions where a contract leaves room, written into the check docstrings:

- Start, pause, resume, return to dock and the emergency stop have no capability flag; every adapter takes them (ADR 0066's minimal adapter). The fake mower is online and at rest when a check begins.
- "Taken" means `executed` or `accepted`; the fake answers with the vendor's behaviour, so a vendor that cannot pause would have to say so in a capability first (none exists today).
- An emergency stop with nothing to stop is not a refusal. On a mower the vendor reports offline the stop must reach the adapter; the vendor may then answer that the mower is offline, so a refusal with a reason passes (the hardware checklist asks that the stop "is still sent", not that it succeeds).
- The fake cloud refuses a changed secret like the vendor would.
- A connection that declares start plans validates the reference, so an unknown one is refused (the amendment says the connection validates; the controller does not).
- The SDK-free spec import is a "should" in `mowdeck_control.providers`; the kit enforces it, and a package that cannot meet it yet lists it as a known violation.
- Lexicon entries need English only; German is MowDeck's second language and not required (ADR 0067), unlike provider texts (ADR 0066 amendment).

The kit runs in the three adapters' tests (`test_mammotion_conformance.py`, `test_husqvarna_conformance.py`, `test_worx_conformance.py`), each on its existing fake cloud, and in its own tests against the simulator, a conforming minimal adapter and one deliberately broken adapter per contract it guards. It found:

1. **Husqvarna offered no start.** Its map has no areas and it declared no start plans, so no client could start a Husqvarna mower. Fixed in the adapter: it declares `duration` (kind `now`), one `work_area:<id>` per work area of models with work areas (kind `zone`, the vendor's name) and `schedule` (kind `schedule`), the references `action_for_plan` already accepted.
2. **The Mammotion spec imports PyMammotion.** The package `__init__` imports the connections, which import the SDK, so loading the spec loads PyMammotion. Harmless while the provider is on by default (the gateway creates it anyway); a lazy package `__init__` would fix it. Listed as a known violation (strict xfail).

### The provider template: `templates/provider` (Copier)

A Copier template next to the service template renders `libs/mowdeck-control-<vendor>`: `pyproject.toml` with both entry points and `[tool.mowdeck.adapter]`, a cloud provider plugin whose spec imports no SDK, a descriptor with English and German texts, a minimal connection (state; start through the start plan `now`, pause, resume, return to the dock, the emergency stop as an immediate pause sent even when the vendor reports the mower offline; nothing else declared), a small port to the vendor cloud with a fake client (one online and one offline mower) and a stub for the real client, an empty error lexicon, a README and the conformance kit wired into its tests, which pass as generated. `uv run poe new-provider <vendor>` runs it.

The licence is a conscious choice: the question `sdk_licence` (permissive or GPL, read from the SDK's sdist) has no default, rendering without it fails, a GPL SDK makes the package `GPL-3.0-only` with the GPL text, a permissive one keeps it Apache-2.0 ([ADR 0005](0005-licensing.md), [ADR 0011](0011-gpl-package-for-pymammotion-adapter.md)). A Copier task copies the licence text from `LICENSES/` (hence `--trust`, as for the service template), so the repository keeps one copy of each text.

`tools/repo-checks/tests/test_provider_template.py` renders the template (Apache and GPL), makes the result importable without installing it (its `src` on the path and a `.dist-info` with the entry points of its `pyproject.toml`, which is all `importlib.metadata` needs), runs its tests in a fresh interpreter and runs ruff and mypy (strict) on it, so the template cannot rot.

Copier because the repository already uses it (`templates/service`, `copier` in the dev group): no new dependency, the same answers file and update path. Alternatives considered:

- *A module of `mowdeck-control` (`mowdeck_control.testing` grows the suite):* rejected, it would make pytest a runtime dependency of the gateway image or need an optional extra that every adapter test must remember.
- *Checks in `tools/repo-checks` that iterate over all adapters:* rejected as the only place, because a package outside the monorepo could not run them and the fake clients belong to each package; the repository checks keep what only the repository can see (the core never imports a vendor).
- *Cookiecutter or a hand-written render script:* cookiecutter would be a second template engine for the same job; a script would re-implement Copier's questions, validation and answers file.
- *Copying the licence texts into the template:* two more copies of texts that already live in `LICENSES/`.
- *Running the rendered package's tests in a fresh virtual environment:* closer to an install but slow and needs the package index; the `.dist-info` route exercises the same entry point discovery.

## Consequences

- A new vendor starts from a package that already passes every check, with the licence decided at the first question; a vendor with few features stays a small package that shows only what it supports.
- Every adapter proves the same contracts on its own fake client; a capability declared without its implementation (or a command offered and refused) fails in the adapter's own CI job, and so does a vendor library update that breaks one (the Dependabot PRs run `libs/`).
- The kit itself is tested against broken adapters, so a check that stops catching its violation fails there.
- Contracts the kit cannot see stay with each package's own tests: the vendor's state mapping, rate limits, login guards, the exact vendor commands.
- New contracts should get a check in the kit when they are made; the checks cite their source, so a changed ADR points at the check to change.
- Follow-ups: a lazy package `__init__` in the Mammotion adapter (then drop its known violation); the Husqvarna start plans want a hardware check (step 4 of its checklist already covers starts by plan).
