# Adding a provider: template and conformance kit

A mower vendor joins MowDeck as one package, `libs/mowdeck-control-<vendor>`, that plugs in through entry points ([ADR 0066](../adr/0066-cloud-provider-plugins-and-more-vendors.md)). This guide goes from an empty directory to a registered provider: generate the package from the template, keep the provider conformance kit green while the adapter grows, then register it. The design behind both tools is [ADR 0069](../adr/0069-provider-conformance-kit-and-package-template.md); how providers fit into the gateway, Fleet and the web app is [Cloud providers: adding a vendor](cloud-providers.md).

The rule throughout: MowDeck shows exactly what the mower can do. Declare a feature in `MowerCapabilities` only together with its implementation; everything you leave out is refused by the controller and hidden by the web app. A vendor with few features is a small package, not an unfinished one.

## 1. Check the vendor SDK's licence

Read the `LICENSE` file inside the SDK's sdist, not only its PyPI metadata (two of the existing adapters wrap SDKs whose metadata says MIT while the file says GPL-3.0). A package that imports a GPL library must be GPL-3.0 itself and stays in its own package ([ADR 0005](../adr/0005-licensing.md), [ADR 0011](../adr/0011-gpl-package-for-pymammotion-adapter.md)); a permissive SDK (MIT, BSD, Apache-2.0) keeps the package Apache-2.0. The template asks for this and has no default.

## 2. Generate the package

```bash
uv run poe new-provider acme
```

Copier asks for the vendor's display name, the SDK's import name (`vendor-modules`, which the core may never import), its PyPI name, its licence (`permissive` or `gpl`) and further brand words. The questions are listed in [`templates/README.md`](https://github.com/lukislp/mowdeck/blob/main/templates/README.md). You get:

| File | What it is |
| --- | --- |
| `pyproject.toml` | entry points `mowdeck.cloud_providers` and `mowdeck.error_lexicons`, `[tool.mowdeck.adapter]`, the licence you chose |
| `src/.../plugin.py` | `SPEC`, importable without the SDK (the gateway loads disabled specs too) |
| `src/.../provider.py` | the `CloudProviderPlugin`: descriptor, discovery, connections, the fake or the real client |
| `src/.../descriptor.py` | credential fields (e-mail and password to start with), guidance, every text in English and German |
| `src/.../connection.py` | the `MowerConnection`: capabilities, state mapping, commands |
| `src/.../client.py` | the port to the vendor cloud; no SDK types above it |
| `src/.../sdk_client.py` | the real client, a stub until you wire in the SDK |
| `src/.../fake_client.py` | the fake cloud: accounts `*@fake.mowdeck.test`, password `fake-cloud-password`, one online and one offline mower |
| `src/.../lexicon.py` | the error lexicon, empty |
| `tests/test_acme_conformance.py` | the conformance kit on the fake cloud |
| `tests/test_acme_connection.py` | the rules the kit cannot know |

The generated mower can do almost nothing: it reports its state and takes start (the start plan `now`, because the fake shares no map), pause, resume, return to the dock and the emergency stop. That is a complete MowDeck provider already.

## 3. Wire it into the workspace and run the kit

Add `"mowdeck-control-acme"` to the dev group and to `[tool.uv.sources]` of the root `pyproject.toml`, and `libs/mowdeck-control-acme` to the mypy `files`; then:

```bash
uv sync
uv run pytest libs/mowdeck-control-acme
uv run poe lint
```

The kit (`mowdeck-provider-testkit`) runs the same suite every adapter runs: entry points and descriptor texts, discovery with the fake credentials and a refused secret, the connection's lifecycle and states, the commands the capabilities offer and the ones they do not, the settings round trip, the emergency stop rules and the lexicon. Its full list with the source of each check is in the [kit's README](https://github.com/lukislp/mowdeck/blob/main/libs/mowdeck-provider-testkit/README.md). Skipped tests say why (no settings declared, no start plans, ...); they turn on by themselves when you declare the feature.

## 4. Grow the adapter, keep the kit green

1. **The real client.** Implement `sdk_client.login` with the SDK, mapping its models onto `VendorMower` and its errors onto `LoginRefused`, `VendorUnavailable` and `MowerOffline`; uncomment and pin the SDK in `pyproject.toml`. Share one logged-in session per vendor account between that account's mowers (the existing adapters keep an account pool), listen to the vendor's push channel instead of polling, never log a credential.
2. **The fake.** Grow `fake_client.py` with every behaviour you rely on, from recorded or handwritten payloads: no test may need a network or a vendor account.
3. **Credentials.** Change the descriptor's fields to what the vendor really needs (an application key and secret, a brand choice, ...). Exactly one field is the identity, at least one is secret, every text has `en` and `de`.
4. **Capabilities.** For each feature the vendor offers, declare it in `capabilities()` and implement it in the same change: map areas (or more start plans: zones, edge cut, schedule), blade height range, light, settings from `SETTING_KEYS` with `SettingsCapable` ([ADR 0046](../adr/0046-device-settings-as-capabilities.md)), `dock_keeps_job`, manual driving with a `DriveSession`, firmware, maintenance. The kit fails when a declared feature is refused or misses its protocol.
5. **State.** Map the vendor's status onto `MowerActivity`, set `raw.lifted` for lift codes and `drive_recoverable` only for a stuck mower, never for a sensor stop ([ADR 0027](../adr/0027-health-history-and-theft-alarm.md)); leave `heartbeat_at`, `cloud_provider` and `connected` to the gateway.
6. **Lexicon.** Fill `ERROR_TITLES` from the SDK's code table ([Adding an error lexicon for a vendor](error-lexicon.md)) and take `error_category` from it, so lexicon and state agree. Hand the kit the table in `emitted_error_codes`; it then checks that every code is explained.
7. **Safety.** The emergency stop acts at once (even while another command runs) and is sent even when the vendor reports the mower offline; a start or resume during an error raises `SensorStop`. Everything else (expiry, de-duplication, dead-man switch, capability checks) is the controller's, not yours ([ADR 0012](../adr/0012-mower-control-interface-and-safety-rules.md)).

A contract you cannot meet yet goes into `ProviderUnderTest.known_violations` with the reason; it runs as a strict xfail, so the list cannot outlive the fix.

## 5. Register the provider

1. Add the package to `services/device-gateway/pyproject.toml` (the only service that installs adapters) and run `uv lock`.
2. Register the vendor library for hardware-tested updates: `VENDOR_LIBS` in `tools/repo-checks/scripts/vendor_lib_guard.py`, `exclude-dependencies` in `.github/workflows/dependabot-auto-merge.yml`, and a checklist in [Vendor library updates](../ops/vendor-lib-updates.md).
3. Document it: the package README (what the user provides, capabilities, limits, rate limits, the fake accounts, "untested on real hardware"), a row in [Cloud providers](cloud-providers.md) and in [Libraries](libraries.md), and `LICENSING.md` for a GPL package.
4. The repository checks then cover it too: `test_vendor_isolation.py` keeps the vendor out of the core and `test_provider_texts.py` checks the descriptor's languages.
5. It stays off by default (`enabled_by_default=False`, `tested_on_hardware=False`) until someone tested it on a real mower; operators enable it with `MOWDECK_CLOUD_PROVIDERS_ENABLE=acme` (Compose: `GATEWAY_CLOUD_PROVIDERS_ENABLE`).

## Running the kit outside the monorepo

The kit only needs the package installed (its entry points) and `pytest-asyncio` in auto mode:

```python
from mowdeck_provider_testkit import ProviderConformance, ProviderUnderTest


class TestAcmeConformance(ProviderConformance):
    subject = ProviderUnderTest(
        distribution="mowdeck-control-acme",
        fake_credentials={"email": "garden@fake.mowdeck.test", "password": "fake-cloud-password"},
    )
```

A package without a cloud provider (only `mowdeck.connections`) passes `connection_factory` instead of `fake_credentials`. Single checks can be called directly from `mowdeck_provider_testkit.checks` with `open_mower(subject, installed(name))`.
