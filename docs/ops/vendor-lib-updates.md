# Vendor library updates

The mower adapters talk to real mowers through one vendor library each, pinned exactly in the adapter's `pyproject.toml`:

| Library | Adapter | Licence of the adapter |
| --- | --- | --- |
| `pymammotion` | `libs/mowdeck-control-mammotion` | GPL-3.0 |
| `pyworxcloud` | `libs/mowdeck-control-worx` | GPL-3.0 |
| `aioautomower` | `libs/mowdeck-control-husqvarna` | Apache-2.0 |

A bug in a new release can break a command, in the worst case the emergency stop or a plain stop. So the owner tests every update on real hardware first and only then merges it by hand. Nothing merges a vendor library update automatically.

## How an update arrives

1. **Dependabot opens one pull request per new release** (`.github/dependabot.yml`, uv entry, `fix(deps)` prefix). The vendor libraries are not ignored and not grouped, so each release is visible on its own. The uv entry allows ten open pull requests, enough for all three vendor PRs to wait for their test alongside the routine updates. Dependabot proposes the newest release, not every release in between.
2. **`vendor-lib-label.yml` labels the PR** `vendor-lib` and `needs-hardware-test` when the PR is opened or reopened. This applies to any PR whose `uv.lock` changes the version of a vendor library, Dependabot's included. A PR that only adds a new adapter with its library is no update and gets no label.
3. **The check `vendor-lib / hardware test`** (`vendor-lib-guard.yml`) fails while the PR updates a vendor library and either carries `needs-hardware-test` or has no `vendor-lib` label yet. Its job summary lists the updated libraries and the next steps. Every other PR passes at once, so the check can be a required check without holding back routine updates.
4. **CI runs the adapters' unit tests against the new release**: the `python` job installs the PR's `uv.lock` and runs every test under `libs/`. That includes the controller path of each adapter with `EmergencyStop`, `PauseMow`, `ReturnToDock` and `StartMow` against the fake vendor client built from the library's real data classes. A red test there is a regression in the library or in the adapter's use of it. Fix the adapter in the same PR or close the PR. The Mammotion adapter also records the release it was tested against (`PYMAMMOTION_VERSION` in `mowdeck_control_mammotion/__init__.py`), and `test_mammotion_package_is_gpl_and_pins_pymammotion` fails until that constant matches the pin. Update the constant together with the test result, after the hardware test.
5. **`dependabot-auto-merge.yml` never approves or merges it** (`exclude-dependencies: pymammotion,pyworxcloud,aioautomower`, also inside grouped PRs).

## What the owner does

1. Check out the PR branch on the machine that reaches the mower (`gh pr checkout <number>`, `uv sync`), or deploy it to the test stack.
2. Run the checklist below for the adapter of the updated library, safety commands first. If a safety command fails, stop there.
3. **Passed:** write the result into the PR body (library version, mower model and firmware, date, the rows of the checklist). For pymammotion, also raise `PYMAMMOTION_VERSION` (see above). Remove the label `needs-hardware-test` and keep `vendor-lib`. The check runs again on the label change and passes. Then approve and merge by hand, or enable auto-merge after the approval, in which case `dependabot-auto-merge.yml` only keeps the branch up to date.
4. **Failed:** keep the label, write what failed into the PR body and close the PR. Dependabot then skips this release and proposes the next one. If the adapter needs a change for the new release, make it in a separate PR that bumps the pin as well.

Removing the label is the record that the hardware test passed. Never remove it without the test. A rebase or a branch update does not put the label back; only reopening the PR does.

## Hardware test checklist

Test outdoors on the owner's mower, with the vendor app at hand as a fallback. Each adapter lists only the commands it supports. Rows marked "safety" must pass before anything else is tried.

### Mammotion (`pymammotion`, LUBA 2 AWD X; cloud and BLE bridge where set up)

| # | Step | Expected | Safety |
| --- | --- | --- | --- |
| 1 | `EmergencyStop` while mowing | wheels and blades stop at once (pause, zero movement, blades off) | yes |
| 2 | `PauseMow` while mowing, then `ResumeMow` | work mode pause, then working again | yes |
| 3 | Manual drive (cloud, and BLE if bridged): drive, then let go of the stick; then close the app or drop the network mid-drive | the mower stops on release (the controller's dead-man `stop()` sends a zero frame) and after the frame ttl (watchdog); it never keeps driving | yes |
| 4 | `ReturnToDock`, and `ReturnToDock(end_job=True)` | returning, then docked or charging; with `end_job` the job does not resume | yes |
| 5 | `StartMow` of one area | route accepted, mowing starts, progress reported | |
| 6 | State while docked and while mowing | activity, battery, charging, RTK status and source, position (the dock pose while docked), firmware | |
| 7 | Lift the mower briefly while it stands (blades off) | a lift or tilt fault, `raw.lifted`, theft alarm notification | |
| 8 | Error code mapping: a fault the mower reports (for example the lift above, or a trapped mower) | code, English text and category as before; the error lexicon still has every code (`test_mammotion_lexicon` passes) | |
| 9 | Owner script `uv run python libs/mowdeck-control-mammotion/scripts/try_real_mower.py` | connects, prints capabilities, state and the map | |

### Worx (`pyworxcloud`; Landroid, Kress, LandXcape)

| # | Step | Expected | Safety |
| --- | --- | --- | --- |
| 1 | `EmergencyStop` while mowing | the mower pauses at once, blades and wheels stop (the cloud has no hard stop) | yes |
| 2 | `PauseMow`, then `ResumeMow` | status pause (34), then mowing again | yes |
| 3 | `ReturnToDock` | going home, then home or charging | yes |
| 4 | `StartMow` with `now`, a `zone:<n>` (if offered) and `edge` (if supported) | mowing starts at that zone, or the edge cut starts | |
| 5 | State | activity, battery, charging, Wi-Fi signal, firmware, rain sensor, GPS position (models with GPS or RTK) | |
| 6 | Lift the mower or turn it over (blades off) | codes 2 or 11, `raw.lifted`, theft alarm notification | |
| 7 | Error code mapping (a rain delay, a lift, a trapped mower) | Worx code, title and category as before | |
| 8 | `StartMow` or `ResumeMow` while the mower reports an error; `EmergencyStop` while the cloud reports it offline | start and resume refused (`SensorStop`); the emergency stop is still sent | yes |

No manual driving: the Worx cloud has no such command.

### Husqvarna (`aioautomower`; Automower, Gardena smart)

| # | Step | Expected | Safety |
| --- | --- | --- | --- |
| 1 | `EmergencyStop` while mowing | the mower pauses at once (the Automower Connect API has no hard stop) | yes |
| 2 | `PauseMow`, then `ResumeMow` (the schedule) | paused, then mowing per schedule | yes |
| 3 | `ReturnToDock` (park) | going home, then parked or charging | yes |
| 4 | `StartMow` by plan (a duration, a work area if the model has them) | mowing starts | |
| 5 | State over the websocket | activity, battery, error, GPS position (models with GPS), derived blade state; the refresh stays within the API limits (no 429) | |
| 6 | Lift the mower (blades off) | lift codes (14, 15, 71, 131), `raw.lifted`, theft alarm notification | |
| 7 | Error code mapping | code, text and category as before; `test_husqvarna_lexicon` passes (the copy of aioautomower's table equals the new release's) | |

No manual driving: the Automower Connect API has no such command.

## Adding a vendor library

A new adapter declares its library in `[tool.mowdeck.adapter] vendor-modules`. Add it to `VENDOR_LIBS` in `tools/repo-checks/scripts/vendor_lib_guard.py`, to `exclude-dependencies` in `.github/workflows/dependabot-auto-merge.yml` and to the tables on this page. `tools/repo-checks/tests/test_dependabot.py` fails until the first two match.
