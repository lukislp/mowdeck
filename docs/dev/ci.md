# CI and pull request scope

`.github/workflows/ci.yml` checks every pull request and every push to `main`. A pull request runs only the jobs its changes need; pushes to `main` and manual runs (`gh workflow run ci.yml`) are not scoped and run what they always ran (the fast jobs on a push, everything by hand).

## How a pull request is scoped

The first job, `changes`, lists the files the pull request changes (the diff between the merge commit and the base branch) and maps each path to the jobs that check it with `tools/repo-checks/scripts/ci_scope.py`. It writes one output per job (`true` or `false`) and a table "CI scope" with the reason for every job into the run's summary. Every other job waits for `changes` and runs unless its output is `false`.

| Changed path | Jobs that run (besides `ci-config`) |
| --- | --- |
| `.github/**` (workflows, Dependabot, templates, the release policy), `socket.yml`, `osv-scanner.toml`, `.pre-commit-config.yaml`, `.clusterfuzzlite/**`, repo-checks tests and CI-only scripts | none; for `ci.yml` the jobs whose definition changed (below) |
| `docs/**`, `mkdocs.yml`, `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `GOVERNANCE.md`, `ROADMAP.md` | docs |
| `infra/helm/**` | helm |
| `tests/load/**` | compose |
| `apps/mobile/**` | web (+ gradle-wrapper for the Gradle wrapper) |
| `apps/web/**`, `libs/mowdeck-ui/**`, `tools/ts-codegen/**`, `services/matter-server/**`, `package.json`, `pnpm-lock.yaml`, `.node-version`, ESLint/Stylelint/Prettier configuration | web, docker, camera-relay-media, e2e, e2e-web, e2e-demo (`apps/web/openapi/**` also python and docs) |
| `services/**`, `libs/**` | python, docs, docker, camera-relay-media, e2e, e2e-web, e2e-demo (`libs/**` also openfga-model) |
| `pyproject.toml`, `uv.lock`, `.python-version`, `infra/compose/**`, `infra/mirror-env.sh`, `infra/openfga/**`, `infra/billing/**`, `infra/entitlements/**` | every job except web and gradle-wrapper |
| `tests/e2e/**` | python, e2e |
| `tests/e2e-web/**` | web, compose, e2e-web, e2e-demo |
| `LICENSE`, `LICENSING.md`, `LICENSES/**` (copied into every image) | docs and the image and stack jobs |
| anything else | every job |

The authoritative list is `RULES` in `ci_scope.py` (first match wins); `tools/repo-checks/tests/test_ci_scope.py` tests it, including the fail-safe cases.

**Fail safe.** A path no rule knows runs every job. A failed `changes` job leaves its outputs empty, and since the jobs only skip on an explicit `false`, they all run. When `ci.yml` itself changes, the script compares the two versions job by job (with the YAML anchors resolved, so a changed shared step counts for every job that uses it): a changed job runs, a change outside `jobs` (`on`, `env`, `permissions`, `concurrency`) or of a job it does not know runs everything.

**`ci-config`** runs on every pull request: [actionlint](https://github.com/rhysd/actionlint) (pinned release, checked against its SHA-256; it runs shellcheck on every `run:` block), shellcheck of the CI shell scripts, [`reuse lint`](https://reuse.software/) (pinned version; every file names its licence and copyright, in an SPDX header or in `REUSE.toml`, see CONTRIBUTING.md, "Licence headers"), and, when the python job is skipped, ruff, mypy and the tests of `tools/repo-checks`, which check the workflows, Dependabot, the release configuration and the other repository conventions. When the python job runs, it runs those tests itself.

The jobs that start the Compose stack (`camera-relay-media`, `e2e`, `e2e-web`, `e2e-demo`) build the images from the cache the `docker` job fills. They wait for it (and for `python` or `web`) when those run, and also run when those are skipped, for example when only their own definition in `ci.yml` changed.

## Coverage in the job summary

The `python` and `web` jobs write their test coverage into the run's summary (`tools/repo-checks/scripts/coverage_summary.py`):

- **python**: statement and branch coverage of the product code from `uv run poe test` (pytest-cov with branch coverage). Test modules, fixtures and `tests/` are omitted in `[tool.coverage.run]` of the root `pyproject.toml`, so they do not inflate the number.
- **web**: Vitest's v8 coverage of the modules listed under `coverage.include` in `apps/web/vite.config.ts` (the logic modules; components and pages are covered by the Playwright gate of `tests/e2e-web`). That list has a line threshold of 80 %.

Neither number is a gate beyond the web threshold; they are there to be visible on every run, for reviews and for the [OpenSSF Best Practices](https://www.bestpractices.dev/) answers.

## Required checks

The branch ruleset of `main` requires `python`, `openfga-model`, `docs`, `web`, `compose`, `helm`, `docker`, `camera-relay-media`, `e2e` and `e2e-web` from this workflow (plus `review / dependency-review`, the Socket Security checks and CodeQL, which are not scoped). GitHub counts a job skipped by its `if:` as passed, so a skipped required job does not block the merge. A workflow that does not start at all (a `paths` filter under `on:`) would leave its required checks pending forever, which is why `ci.yml` has no `paths` filter and always starts; `test_ci_scope.py` keeps it that way. `e2e-web`, which merges the shard reports, is skipped together with its shards when the suite is not needed and still fails when a needed shard failed or was skipped.

Other workflows that only matter for some paths keep their own `paths` filter, because none of them is a required check: `helm-kind.yml` (the chart), `fuzzing.yml` (fuzzed code), `mirror-images.yml` (the image list), `reproducibility.yml` (the build inputs, see below).

## Reproducible build check

`.github/workflows/reproducibility.yml` builds the same commit twice from a clean tree and compares every output file byte for byte: the Python packages, the web app, the Helm chart and every released image. It runs weekly, on demand and on pull requests that change build inputs. Any difference fails it. See [Reproducible builds](reproducible-builds.md).

## Releases from CI-only changes

A pull request that only changes CI configuration releases nothing (details in [Releases](../ops/release.md#ci-only-changes)): Dependabot's GitHub Actions and ClusterFuzzLite bumps are `ci:` commits, `fix(ci):` is not a release type in `.github/lktec-bot.yml`, and the release workflow skips a release whose changes since the last tag are CI configuration only.
