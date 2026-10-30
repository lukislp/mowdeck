# mowdeck-provider-testkit

The provider conformance kit ([ADR 0069](../../docs/adr/0069-provider-conformance-kit-and-package-template.md)): one vendor-neutral pytest suite that every MowDeck adapter package runs against itself, on its own fake vendor client, to prove that it honours the contracts of [ADR 0012](../../docs/adr/0012-mower-control-interface-and-safety-rules.md) (connection interface and safety rules), [ADR 0046](../../docs/adr/0046-device-settings-as-capabilities.md) (settings as capabilities), [ADR 0066](../../docs/adr/0066-cloud-provider-plugins-and-more-vendors.md) (cloud provider plugins, "no dead buttons") and [ADR 0067](../../docs/adr/0067-error-code-lexicon.md) (error lexicons).

Licence: Apache-2.0. A test-only library: adapters list it in their test dependencies, never at runtime. It depends on `mowdeck-contracts`, `mowdeck-control`, `pytest` and `pytest-asyncio`, and it imports no vendor package and names no vendor: it learns everything from the installed distribution under test (its entry points and `pyproject.toml`).

## Use

```python
# libs/mowdeck-control-acme/tests/test_acme_conformance.py
from mowdeck_provider_testkit import ProviderConformance, ProviderUnderTest

from mowdeck_control_acme.fake_client import FAKE_EMAIL, FAKE_PASSWORD


class TestAcmeConformance(ProviderConformance):
    subject = ProviderUnderTest(
        distribution="mowdeck-control-acme",
        fake_credentials={"email": FAKE_EMAIL, "password": FAKE_PASSWORD},
    )
```

That is all a cloud provider supplies: the kit creates the plugin with `ProviderContext(fake=True)`, discovers the fake account, builds the connection of its first online mower exactly as the Device-Gateway does (device name, IoT id and credentials in the options) and wraps it in `MowerController`. The async tests need `pytest-asyncio` with `asyncio_mode = "auto"`.

`ProviderUnderTest` options:

| Field | Meaning |
| --- | --- |
| `distribution` | the installed distribution, `mowdeck-control-<vendor>` |
| `fake_credentials` | credentials the fake cloud accepts (by credential field key) |
| `connection_factory` | builds a connection on the fake client yourself (packages without a cloud provider, or a special fake); wins over the provider path |
| `pick_device` | which discovered device to test (default: the first online mower) |
| `mower_options` | extra connection options, for example short confirmation timeouts |
| `emitted_error_codes` | every code the adapter can report (usually the SDK's table); the lexicon must explain each |
| `required_locales` | locales every provider text must have (default `en`, `de`: the web app's languages) |
| `command_timeout_s`, `emergency_stop_timeout_s` | bounds on the fake client (30 s, 5 s) |
| `known_violations` | test name to reason: runs as a strict xfail until fixed |

The checks are plain functions in `mowdeck_provider_testkit.checks` (they raise `AssertionError`), so a package can also call one directly with `open_mower(subject, installed(name))`.

## What it checks

| Test | Source | Check |
| --- | --- | --- |
| `test_package_layout` | ADR 0066 | named `mowdeck-control-<vendor>`, import package installed |
| `test_entry_points_declared` | ADR 0066 decision 6, ADR 0067 | registers `mowdeck.connections` or `mowdeck.cloud_providers`, ships `mowdeck.error_lexicons`, every entry point points into its own package |
| `test_entry_points_load` | ADR 0012, 0066, 0067 | every entry point loads through `mowdeck_control`'s own loaders with matching names (kind, provider id, vendor key) |
| `test_vendor_modules_declared` | ADR 0066 decision 6 | `[tool.mowdeck.adapter] vendor-modules` lists the SDK, `brand-names` is a list |
| `test_no_other_adapter_imported` | ADR 0066 decision 6 | no import of another adapter package |
| `test_provider_spec_imports_without_vendor_sdk` | `mowdeck_control.providers` | the spec imports with the vendor modules blocked (the gateway loads disabled specs too) |
| `test_provider_descriptor` | ADR 0066 decisions 1, 2, text amendment | valid id, descriptor names the same provider, on by default only when tested on hardware, JSON round trip, every text in `en` and `de`, the fake credentials fit the fields |
| `test_provider_discovery` | ADR 0066 decision 1 | the fake account lists a mower; a wrong secret raises `CloudLoginRefused` without repeating it |
| `test_connection_lifecycle` | ADR 0012 | `is_connected` only between connect and disconnect, both idempotent, `watch_state` starts with the current state and ends on disconnect |
| `test_capabilities_and_state` | ADR 0012, 0046, 0067 | the connection's kind is in `connection_kinds`; each declared optional feature implements its protocol (settings, maintenance, undock, firmware, base station, drive speed, camera, bridge scan); the state belongs to the mower, sets no gateway-owned field, never marks a sensor stop drive recoverable, reports only declared settings with valid values, and its error category agrees with the lexicon |
| `test_mower_can_be_started` | ADR 0066 amendment | map areas or `start_plans`: a client can offer a start |
| `test_base_job_flow` | ADR 0012, ADR 0066 | start, pause, resume, emergency stop, return to dock, return ending the job: each executed or accepted, every state on the way well-formed |
| `test_undeclared_commands_refused` | ADR 0066 principle, ADR 0012 | every command the capabilities do not offer (manual drive, blade height, speed, light, keeping the job, firmware, maintenance, an undeclared setting, an unknown area, an undeclared pattern) is refused with a reason before the adapter is asked |
| `test_declared_commands_accepted` | ADR 0066 principle | blade height and speed within the declared ranges, the light: taken by the adapter |
| `test_start_plans` | ADR 0066 amendment | every declared start plan starts the mower; an unknown plan is refused |
| `test_keep_job_on_return` | ADR 0066 amendment | `dock_keeps_job` declared: `ReturnToDock(end_job=False)` taken during a job |
| `test_native_pattern_start` | ADR 0012 | a declared native pattern starts on a map area |
| `test_settings_round_trip` | ADR 0046 | every declared setting takes a valid value; `executed` means the state shows it |
| `test_manual_drive` | ADR 0012 | without `manual_drive` a drive session is refused with `CommandRejected`; with it a session opens, takes a zero frame, `stop` and `close` are idempotent |
| `test_emergency_stop_at_rest` | ADR 0012 | executed or accepted at rest |
| `test_emergency_stop_while_busy` | `MowerConnection` rules | returns within 5 s while a start is still in flight |
| `test_emergency_stop_offline_mower` | ADR 0012, `docs/ops/vendor-lib-updates.md` | not refused on a mower the fake account lists as offline |
| `test_lexicon` | ADR 0067 | JSON round trip, English titles, each code finds its entry, the lexicon is found by the package's provider ids and connection kinds |
| `test_lexicon_covers_emitted_codes` | ADR 0067 | every code the adapter can report is explained |

Assumptions where a contract leaves room are listed in ADR 0069; the docstrings of `checks.py` name the source of each check.

## Tests

`tests/` runs the whole suite against the simulator of `mowdeck-control` and against small broken adapters, so a check that stops catching a violation fails here.
