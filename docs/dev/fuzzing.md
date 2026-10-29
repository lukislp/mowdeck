# Fuzzing and property-based tests

MowDeck parses a lot of input it does not control: frames of the mower (cloud and Bluetooth bridge), records of the camera source bridge, bearer tokens and relay tickets, NATS messages, browser WebSocket frames, third-party API answers (Open-Meteo, vision models) and what users draw and configure. Two kinds of tests throw generated input at these parsers:

- **Coverage-guided fuzzing** of the Python code with [Atheris](https://github.com/google/atheris) (libFuzzer for Python), run continuously by [ClusterFuzzLite](https://google.github.io/clusterfuzzlite/) in GitHub Actions;
- **Property-based tests** of the web app's TypeScript with [fast-check](https://fast-check.dev/), part of the normal Vitest suite.

Both are what the OpenSSF Scorecard "Fuzzing" check looks for (`checks/raw/fuzzing.go`: a `.clusterfuzzlite/Dockerfile`, an `import atheris` line in a Python file, an import of `fast-check` in a TypeScript file).

## Fuzz targets

One module per target in `fuzz/`, each with the properties it checks in its docstring. A target raises (an `AssertionError` or the unexpected exception) only when the code under test breaks its contract; libFuzzer reports that as a crash.

| Target | Code under test | Properties |
| --- | --- | --- |
| `fuzz_bridge_protocol` | Camera source bridge protocol v1: `agora_bridge.protocol` (control lines, records) and the relay's reader `camera_relay.adapters.media.bridge_protocol` | only the protocol errors; the same records however the stream is split; both readers agree (differential); frames round-trip |
| `fuzz_relay_ticket` | Camera relay tickets (WHEP bearer tokens) | only `InvalidTicketError`; only tickets of the key verify, in exactly one spelling; signed claims round-trip until they expire |
| `fuzz_access_token` | `mowdeck_auth.JwtVerifier`, Identity's `parse_api_token`, `parse_jwks` | only `TokenError` for any string and any signed claims; one spelling per API token; a JWKS body raises only `ValueError` |
| `fuzz_subjects` | NATS subject parsers of `mowdeck_contracts.v1.subjects`, OpenFGA references of `mowdeck_authz.refs` | only `ValueError`; parsed ids rebuild subjects that parse back and match the subscribers' wildcards; references are stable |
| `fuzz_contracts` | Wire models of `mowdeck_contracts.v1` (Realtime client frames, command bodies and envelopes, `MowerState`, maps, spot plans, ...) | only `ValidationError`; an accepted message survives a JSON round trip |
| `fuzz_polyline` | The spot plan polyline codec | only `ValueError`; canonical re-encoding; encoder and decoder agree on the range |
| `fuzz_vision_answer` | `extract_json` and `AssessmentOut` of the vision service, the proposal rules | only the errors the provider handles; a drive maneuver is proposed only without risk flags, above the confidence threshold, within the joystick and time limits |
| `fuzz_open_meteo` | `parse_forecast` of the scheduler | only the errors the provider turns into "weather unknown" |
| `fuzz_mammotion_report` | PyMammotion's decoding of `LubaMsg` frames, then the adapter's `mapping` (`state_of`, `map_of`, `capabilities_of`, `mow_path_of`) | the mapping never raises on whatever device state the frames leave; positions within WGS84, battery within 0..100 % |
| `fuzz_patterns` | `mowdeck_patterns` (`Area.of`, `plan`, the spot plan contract) as the Planner calls it | only `PatternError`; an accepted plan converts to `SpotMowPlan` and back and its preview builds |

`fuzz/harness.py` holds the shared helpers (standard library only): `Input` reads typed values from the fuzz bytes, so the targets need no Atheris API besides `instrument_imports`, `Setup` and `Fuzz`. Each target builds its seed corpus with the code it fuzzes (`seeds()`), and `fuzz/json.dict` is the libFuzzer dictionary of all of them: most parsing happens in C or Rust (`json`, pydantic-core) that Atheris cannot see, so the edge values (huge numbers, `NaN`, year 1 and 9999 timestamps, ...) come from the dictionary.

`tools/repo-checks/tests/test_fuzz_targets.py` keeps them honest in the normal test run: every target has the layout above and is listed on this page, and each one replays its seeds plus a few hundred mutated inputs with a stand-in for Atheris (so it runs everywhere, Windows included). A finding that was fixed gets a regression test next to the code and, where useful, stays covered by the seeds.

## ClusterFuzzLite

`.clusterfuzzlite/` is the build: `Dockerfile` (the OSS-Fuzz Python builder pinned by digest, plus CPython 3.13 from uv because the workspace needs it; Atheris, PyInstaller and coverage come from `requirements.txt` with hashes), `build.sh` (installs the third-party dependencies of the fuzzed packages exactly as locked in `uv.lock`, builds one PyInstaller binary per target in parallel and zips its seeds) and `project.yaml`.

`.github/workflows/fuzzing.yml` runs it:

| Trigger | Mode | Budget |
| --- | --- | --- |
| pull requests that change `fuzz/`, `.clusterfuzzlite/`, `libs/`, `services/`, `integrations/`, `pyproject.toml`, `uv.lock` or the workflow | `code-change`: the targets the change can reach | 8 minutes over all targets |
| daily | `batch`: grows the corpus | 1 hour |
| weekly (Sunday) | `prune` (minimises the corpus), then `coverage` (report) | 10 minutes each |

A crash fails the run; the crashing input (`crash-<sha1>`) and the fuzzer output are uploaded as artifacts of the run, and the crash is uploaded to code scanning (SARIF). Corpora and coverage reports are kept as GitHub Actions artifacts (ClusterFuzzLite's default without a storage repository), so the setup needs no token or repository besides this one. `workflow_dispatch` starts a batch, prune or coverage run by hand.

The artifact store has a price: to find the latest corpus, ClusterFuzzLite pages through all artifacts of the repository (100 per API call) for every target, so one pull request run costs a few hundred calls of `GITHUB_TOKEN`'s 1,000 per hour, and a lookup refused by the rate limit makes the run fuzz silently without its corpus. Hence the path filter above (the check is not required; a pull request that touches no fuzzed code skips it) and the effort to keep the artifact count low: no Docker build record artifacts (`DOCKER_BUILD_RECORD_UPLOAD: "false"` in every workflow that builds images) and seven days or less for CI logs and reports. ClusterFuzzLite's own uploads (corpus, coverage, crashes) keep the repository's default retention (14 days), so the weekly coverage report and the corpus always outlive the gap between two scheduled runs; do not set the default below 8 days. `tools/repo-checks/tests/test_workflow_api_quota.py` guards these settings.

To update the toolchain: edit `.clusterfuzzlite/requirements.in` and compile it again (Linux x86-64 wheels, as in the image):

```bash
uv pip compile .clusterfuzzlite/requirements.in --generate-hashes --python-version 3.13 \
  --python-platform x86_64-manylinux_2_28 --no-header -o .clusterfuzzlite/requirements.txt
```

The builder image digest is bumped deliberately, like the other images; the uv image is the `UV_IMAGE` of `infra/compose/.env.example` (the repo check keeps them equal).

## Running a fuzzer locally

Docker on an x86-64 machine, from the repository root. Build the image and the targets (into the Docker volume `cfl-out`):

```bash
docker build -t mowdeck-cfl -f .clusterfuzzlite/Dockerfile .
docker run --rm -e SANITIZER=address -e FUZZING_LANGUAGE=python -e ARCHITECTURE=x86_64 \
  -e FUZZING_ENGINE=libfuzzer -v cfl-out:/out mowdeck-cfl compile
```

Fuzz one target for five minutes with its seeds and dictionary, crashes into the volume `cfl-crashes`:

```bash
docker run --rm -e FUZZING_ENGINE=libfuzzer -e FUZZING_LANGUAGE=python -e SANITIZER=address \
  -e RUN_FUZZER_MODE=interactive -v cfl-out:/out -v cfl-crashes:/crashes \
  gcr.io/oss-fuzz-base/base-runner \
  run_fuzzer fuzz_relay_ticket -max_total_time=300 -artifact_prefix=/crashes/
```

Without Docker the targets still run as plain Python (no coverage guidance) through the repo check:

```bash
uv run pytest tools/repo-checks/tests/test_fuzz_targets.py
```

## Reproducing a crash

Download the `crash-<sha1>` file from the run's artifacts (or take it from `/crashes`) and feed it to the target:

```bash
docker run --rm -e FUZZING_ENGINE=libfuzzer -e FUZZING_LANGUAGE=python -e SANITIZER=address \
  -v cfl-out:/out -v "$PWD:/testcase" gcr.io/oss-fuzz-base/base-runner \
  reproduce fuzz_relay_ticket -runs=10 /testcase/crash-<sha1>
```

The Python traceback names the failing line. To debug it in the workspace instead, call the target's `test_one_input` with the file's bytes (`atheris` replaced as in the repo check), fix the code, add a regression test with the input next to the code's own tests, and keep the input as a seed when it covers a new path.

## Property-based tests (web app)

`apps/web/src/**/*.property.test.ts` use fast-check inside Vitest (`pnpm --filter web test`):

| Test | Properties |
| --- | --- |
| `lib/legal/markdown.property.test.ts` | the operator's legal Markdown always parses; every rendered link is a web, mail or phone link or stays on this origin (as the browser resolves it) |
| `lib/config.property.test.ts` | any `/config.json` parses without throwing; only `http(s)` tile URLs with the placeholders, zoom 1..22 and an `http(s)` source link come through |
| `lib/camera/whep-player.property.test.ts` | a root-relative WHEP `Location` stays on the page origin and gets the proxy prefix back |
| `lib/map/heatmap.property.test.ts` | the heat layer window never ends before it starts; the mower id stays inside its path segment; the preference guards accept only known values |
| `lib/realtime/backoff.property.test.ts` | the reconnect delay stays between zero and the capped exponential bound |

fast-check prints the seed and the shrunk counterexample of a failure; `fc.assert(..., { seed, path })` replays exactly that case.
