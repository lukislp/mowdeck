# Contributing

MowDeck has one maintainer ([GOVERNANCE.md](GOVERNANCE.md)), so the process is deliberately small, and it is the same for every change, the maintainer's own included. Planned work is in [ROADMAP.md](ROADMAP.md). Security problems never go into a public issue: see [SECURITY.md](SECURITY.md).

## How changes get in

1. **Bugs and requests** go into [GitHub Issues](https://github.com/lukislp/mowdeck/issues/new/choose) with the bug report or feature request template. Questions and ideas go into [Discussions](https://github.com/lukislp/mowdeck/discussions). The maintainer aims to answer new issues and discussions within 14 days. For anything bigger than a small fix, open an issue first, so the direction is agreed before you spend time on it.
2. Fork the repository and make your change on a branch.
3. Open a pull request against `main`. The pull request template asks what changed and why, and has the checklist below.
4. `main` is protected: a pull request merges only after the required checks are green, its commits are signed and the branch is up to date ([CI and pull request scope](docs/dev/ci.md)). Nobody pushes to `main` directly, not even the maintainer.

## Workflow

- **Conventional Commits** for all commits, in English: `feat(identity): add sub-user creation`, `fix(compose): ...`, `docs(adr): ...`, `chore: ...`. Breaking changes use `!` or a `BREAKING CHANGE:` footer.
- **One task at a time.** Each task delivers its code, tests and a README for what it adds.
- The GitHub wiki is a generated mirror of `docs/`: edit the docs, never the wiki (see `docs/dev/wiki.md`).
- Everything is written in English: code, comments, commits, documentation. The UI is German by default, with English available.

## Releases

Releases are automatic: every push to `main` runs semantic-release (`.github/workflows/release.yml`, `.releaserc.json`), and the Conventional Commit types since the last tag decide whether and how the version moves. So the commit type is part of the change, not a formality; with a squash merge it is the pull request title.

- `fix:`, `perf:`, `refactor:` release a patch, `feat:` a minor version.
- MowDeck is on the **0.x line**: a breaking change (`feat!:`, `fix(api)!:` or a `BREAKING CHANGE:` footer) releases a minor version, never 1.0.0. Mark it anyway, the release notes list it under "BREAKING CHANGES". Going to 1.0.0 is a deliberate change of `.releaserc.json` by the owner ([Releases](docs/ops/release.md#commit-types-and-versions)).
- `docs:`, `ci:`, `chore:`, `test:`, `style:`, `build:` release nothing. Use them for changes that ship nothing to an installation, and `fix:` or `feat:` when a user would notice.
- CI-only changes never release: write them as `ci:` (`fix(ci):` releases nothing either), Dependabot's GitHub Actions bumps arrive as `ci: bump ...`, and the release workflow skips a release whose changes since the last tag are CI configuration only ([CI-only changes](docs/ops/release.md#ci-only-changes)). Runtime dependency bumps stay `fix(deps):` and release a patch.
- Pull requests run only the CI jobs their changes need; a CI-only pull request runs `ci-config` (actionlint, shellcheck, repo-checks) and the jobs whose definition it changes ([CI and pull request scope](docs/dev/ci.md)).
- Never tag by hand. The release tag is SSH-signed, images, chart and release files are signed with Sigstore and carry build provenance; how to verify them is in [Releases](docs/ops/release.md#verifying-a-release).

## Definition of done

A change is done when:

1. Tests exist and are green (including tenant-isolation tests where data is touched), as described under [Tests](#tests).
2. Lint is clean (ruff, ruff format, mypy strict; for the frontend ESLint, Prettier, Stylelint, svelte-check and the i18n check).
3. Documentation is updated (affected README, `docs/`), and an ADR is added or updated for every architecture decision.
4. `mkdocs build --strict` passes.

A new service is generated from `templates/service` and has its own definition of done in `docs/dev/service-template.md`. A new mower vendor is generated from `templates/provider` (`uv run poe new-provider <vendor>`) and keeps the provider conformance kit green (`docs/dev/adding-a-provider.md`).

A new third-party image (a base image, an infrastructure container, an image only CI runs) is pinned under "Image sources" in `infra/compose/.env.example` and used through its `*_IMAGE` variable or build argument; `tools/repo-checks/tests/test_images.py` keeps every default equal to that list. CI pulls the list through the repository's GHCR mirror: the workflow `mirror-images.yml` copies a new or bumped image there when the pull request changes the list (re-run CI jobs that pulled before the copy finished). See `infra/compose/README.md`, "Image sources".

## Tests

- **New behaviour comes with tests.** A feature or a change of behaviour adds tests next to the code it changes: `services/<service>/tests`, `libs/<library>/tests`, `*.test.ts` in `apps/web` and `libs/mowdeck-ui`, the Playwright gate in `tests/e2e-web` for user flows, `tests/e2e` for the acceptance test of the whole stack. A pull request that adds behaviour without a test is asked to add one.
- **Every defect fix comes with a regression test.** A fix of a defect in the product's behaviour (`services/`, `libs/`, `apps/`, `bridge/`, `integrations/`) adds a test that fails without the fix and passes with it. Security fixes always get one. Exempt:
  - dependency updates (`fix(deps)`, Dependabot pull requests, lock file refreshes): the existing suites and the required checks cover them instead. The mower vendor libraries also get the hardware test ([vendor library updates](docs/ops/vendor-lib-updates.md));
  - fixes without testable behaviour (documentation, CI, build or signing configuration, pure styling): the pull request says why no test was added and how the fix was verified.
- **Data access is tested across accounts.** A change that reads or writes account data has a test that requests another account's data and expects the rejection ([security assurance case](docs/dev/security-assurance.md)).
- **Input is bounded.** A new route or model field gets bounds in its Pydantic model (or an explicit check in the service) and a test for the rejection ([input validation](docs/dev/security-assurance.md#input-validation)).
- **Parsers of untrusted input are fuzzed.** A new parser of vendor frames, bridge records, tokens or messages gets a fuzz target in `fuzz/` ([fuzzing](docs/dev/fuzzing.md)).

`uv run poe test` runs the Python suites with branch coverage, and `pnpm -r test` runs the frontend suites with coverage. CI shows the numbers in the job summary of `python` and `web` ([CI](docs/dev/ci.md)), and they are expected not to drop.

## Coding style

- **Python:** [PEP 8](https://peps.python.org/pep-0008/), as checked by ruff (rule sets `E`, `F`, `W`, `I`, `N`, `UP`, `B`, `S`, `C4`, `SIM`, `RUF`, `ASYNC`, line length 100) and formatted with `ruff format`. Type hints everywhere, checked by mypy in strict mode. The configuration is in the root `pyproject.toml`. A `# noqa` or `# type: ignore` names the rule and says why.
- **TypeScript and Svelte:** ESLint with typescript-eslint, Prettier, Stylelint and svelte-check (`eslint.config.js`, `.prettierrc.json`, `stylelint.config.js`). UI work uses the design tokens of `libs/mowdeck-ui` and the UI checklist below.
- **Workflows and shell:** actionlint and shellcheck (job `ci-config`). Actions are pinned to a commit SHA with the version in a comment.
- **Everything:** `.editorconfig`, English, comments that explain why rather than what. Decisions go into an ADR ([docs/adr](docs/adr/README.md)).

The required checks enforce all of this: `python` runs `uv run poe lint` (ruff, ruff format, mypy), `web` runs `pnpm -r lint` and `pnpm -r check`, and `ci-config` lints the workflows. A pull request with lint errors cannot merge.

### Licence headers

The repository follows the [REUSE Specification](https://reuse.software/): every file states its licence and copyright. A source file starts with two SPDX tags in its comment syntax (after a shebang or a Dockerfile `# syntax=` line), with the licence of the directory it lives in ([LICENSING.md](LICENSING.md): `AGPL-3.0-only` by default, `Apache-2.0` in `libs/` and `integrations/agora-bridge`, `GPL-3.0-only` in `libs/mowdeck-control-mammotion` and `libs/mowdeck-control-worx`):

<!-- REUSE-IgnoreStart -->
```python
# SPDX-FileCopyrightText: 2026 Your Name
# SPDX-License-Identifier: AGPL-3.0-only
```
<!-- REUSE-IgnoreEnd -->

`uv run --no-project --with "reuse[charset-normalizer]==6.2.0" reuse annotate --copyright "Your Name" --license AGPL-3.0-only <file>` adds them. Files without comments (Markdown, JSON, images, lock files) and generated files are covered by [`REUSE.toml`](REUSE.toml) instead; a generator writes no header, so its output stays byte-identical. `ci-config` runs `reuse lint` and fails for a file without either; run the same command with `reuse lint` locally.

## Code review

Every change, the maintainer's own included, goes through a pull request and the required checks. The reviewer (today the maintainer, see [GOVERNANCE.md](GOVERNANCE.md)) looks at:

1. **Correctness and tests:** the change does what the pull request says, new behaviour has a test, and a defect fix has a regression test that fails without it (or a stated reason for the exemption above).
2. **Security:** authentication and an OpenFGA check on every new route and command kind, input bounds, account scoping and the cross-account test, no secrets or personal data in code, tests or logs, outbound URLs only through an allow list. A change to a trust boundary updates the [security assurance case](docs/dev/security-assurance.md).
3. **Mower safety:** the rules of [ADR 0012](docs/adr/0012-mower-control-interface-and-safety-rules.md) hold. The emergency stop stays reachable and is never blocked or delayed. An adapter change keeps the provider conformance kit green.
4. **Compatibility:** contracts and the generated OpenAPI documents and types, database migrations (backward compatible), the Home Assistant integration and the apps. Breaking changes are marked (`feat!:`).
5. **Operations and docs:** Compose and Helm, the affected README and `docs/`, an ADR for an architecture decision, German and English texts.

Dependabot pull requests go through the same checks. The reviewer reads the release notes of a major update before merging it, and the vendor libraries additionally need the hardware test.

## Developer Certificate of Origin

By contributing you certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/): you wrote the change, or otherwise have the right to submit it under the licence of the part it touches ([LICENSING.md](LICENSING.md): AGPL-3.0, Apache-2.0 for `libs/`, GPL-3.0 for the GPL adapter packages). Sign off every commit with `git commit -s`, which adds a `Signed-off-by: Your Name <email>` line. Automated dependency pull requests are exempt. No check enforces the sign-off today.

## Dependency updates

Dependabot opens the dependency PRs (`.github/dependabot.yml`) and updates `uv.lock` and `pnpm-lock.yaml` itself. The workflow `dependabot-auto-merge.yml` (a caller of the shared `lukislp/ci-workflows` implementation) approves patch and minor updates and merges them once every required check is green; majors stay a manual decision. It needs the `AUTOMERGE_TOKEN` repository secret, a fine-grained token of the code owner with contents and pull requests write on this repository; without it the workflow does nothing.

The vendor libraries of the mower adapters (`pymammotion`, `pyworxcloud`, `aioautomower`) are excluded (`exclude-dependencies: pymammotion,pyworxcloud,aioautomower`), also inside grouped PRs. A bug in a release can break a command, in the worst case the emergency stop, so the owner tests every update on a real mower before merging it by hand. Dependabot opens one PR per release. `vendor-lib-label.yml` labels it `vendor-lib` and `needs-hardware-test`. The check `vendor-lib / hardware test` (`vendor-lib-guard.yml`) fails until the owner removes `needs-hardware-test` after the test. See [Vendor library updates](docs/ops/vendor-lib-updates.md) for the flow and the hardware test checklist. After that test, approve the PR and merge it, or enable auto-merge; the workflow then only keeps the branch up to date.

The Socket Security GitHub App (configured in `socket.yml`) scans every pull request for supply-chain risks in new or changed dependencies, such as install scripts, typosquatting, obfuscated code or known malware. Its checks `Socket Security: Project Report` and `Socket Security: Pull Request Alerts` will be required checks; a flagged dependency is either replaced or explicitly reviewed before the merge.

## Running checks locally

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), Docker.

Linux, macOS, Git Bash:

```bash
uv sync
uv run poe lint
uv run poe test
uv run poe docs
uv run poe compose-check
```

Windows PowerShell: the same commands work unchanged.

```powershell
uv sync
uv run poe lint; uv run poe test; uv run poe docs; uv run poe compose-check
```

Format and auto-fix: `uv run poe fmt`.

## Frontend checks

The web app (`apps/web`), the design system (`libs/mowdeck-ui`), the type generator (`tools/ts-codegen`) and the Playwright gate (`tests/e2e-web`) form a pnpm workspace. Requirements: Node 24 (24.21 or newer, `.node-version`) and pnpm 10.34.6 (`corepack enable`). CI runs the same steps in the job `web`:

```bash
pnpm install --frozen-lockfile
pnpm -r lint
pnpm -r check
pnpm -r test
pnpm --filter ts-codegen check
pnpm --filter web build
```

`pnpm -r test` includes the Playwright suite of `tests/e2e-web`, which needs the throwaway test stack (below); without it, run `pnpm -r --filter '!e2e-web' test`. After changing a service route or a contract, regenerate the API types: `uv run poe openapi` (checked by `uv run poe openapi-check` and `uv run poe test`), then `pnpm codegen`, and commit the results with the change. Details: [docs/dev/web-app.md](docs/dev/web-app.md).

`pnpm.overrides` in the root `package.json` lifts transitive dependencies with a known advisory whose parent has no fixed release yet, each one scoped to that parent: `axios>follow-redirects` (axios 1.20.0 through `agora-rtc-sdk-ng` and `@vueuse/integrations`, GHSA-mrwp-f3q8-3g9p; keeps the lock from resolving a follow-redirects older than 1.16.2) and `xcode>uuid` (`@capacitor/cli`, GHSA-w5hq-g745-h8pq; uuid 11 still ships the CommonJS build xcode requires). Drop an entry once the parent depends on a fixed version itself, then run `pnpm install` and commit the lock file.

Two overrides lift dev-only tools ahead of their parents: `rimraf>glob` (`@capacitor/cli` through rimraf 6, which still requires glob 13; GHSA-3g3m-2fjr-6pw4, fixed in glob 14) and `minimatch@^10` (eslint, typescript-eslint and glob; GHSA-g3hv-75wg-m249, fixed in minimatch 11). The minimatch override leaves the 5.x copy of `@redocly/openapi-core` (through `openapi-typescript` in `tools/ts-codegen`) alone: redocly 1.x calls `require("minimatch")` as a function, and minimatch 9 and later only export it by name.

`osv-scanner.toml` at the repository root records advisories accepted on purpose because no fixed release exists; OSV-Scanner and the OpenSSF Scorecard Vulnerabilities check both honour it. It currently holds one entry: `deepmerge` 4.3.1 (GHSA-gcqx-w48m-w2h4, no fixed release), reached only through the `@sveltejs/vite-plugin-svelte` devDependency, which merges developer-authored build config with it and never ships in the web image. The two `@sveltejs/kit` 2.70.3 advisories it carried earlier (GHSA-27mw-82g8-phjj, GHSA-pfhg-q3qv-9q6r) went with the SvelteKit 3 migration (ADR 0020, addendum of 2026-10-24). Where a parent caps a dependency below its only fixed release, an override (`pnpm.overrides` in `package.json`, `override-dependencies` under `[tool.uv]` in `pyproject.toml`) is preferred to an ignore; `filelock` is lifted past `litellm`'s `<4.0` cap that way. Remove an entry as soon as Dependabot or upstream ships a fixed version, and re-check it before its `ignoreUntil` date at the latest instead of just extending it. Check locally with `docker run --rm -v "$PWD:/src" ghcr.io/google/osv-scanner scan source -r /src`.

### End-to-end tests

The e2e suites (`tests/e2e-web`, `tests/e2e`) create users, accounts and mowers. Run them **only against the throwaway test stack**, never against the stack you or the household use (`http://localhost`, the LAN address):

```bash
pnpm e2e:stack up          # Compose project mowdeck-e2e at http://localhost:8480, own volumes and ports
pnpm e2e:stack test        # Playwright (args go to playwright test); `acceptance` runs tests/e2e
pnpm e2e:stack down        # delete it again; `pnpm e2e:stack run` does up, test, down
```

Both suites refuse a target without the e2e marker (`infra/compose/docker-compose.e2e.yml`). Do not bypass the guard with `MOWDECK_E2E_ALLOW_ANY_STACK=1` against a stack with real data, do not append `tests/e2e-web/stack.env` to `infra/compose/.env`, and in a second worktree pick another `MOWDECK_E2E_PROJECT` and `MOWDECK_E2E_PORT`. Details: [tests/e2e-web/README.md](tests/e2e-web/README.md).

### UI pull requests

A pull request that touches `apps/web` or `libs/mowdeck-ui` attaches screenshots in light and dark theme at phone (390 x 844) and desktop (1280 x 800) width: with the test stack up (`pnpm e2e:stack up`), `pnpm e2e:stack screens` writes the main pages (login, mower list, mower detail in light and dark, mower settings) to `apps/web/screenshots/`; add the changed pages that it does not cover. Its description carries this checklist:

- [ ] Only tokens used; no new colours, radii or fonts outside `tokens.css`
- [ ] Colour is used only for state; every state also has text or a glyph
- [ ] Readouts in Plex Mono with units; labels uppercase 12 px
- [ ] Targets at least 48 px, gaps at least 8 px; checked at 390 px width
- [ ] Emergency stop visible and reachable on every mower view, including with sheets and keyboard open
- [ ] Light and dark screenshots attached; sunlight check (light theme, full brightness) for new status UI
- [ ] No hard-coded strings; German and English texts present and fitting (German is longer)
- [ ] Pending, error, empty, offline and no-permission states designed
- [ ] Keyboard focus visible, `prefers-reduced-motion` respected

## Optional git hooks

`uv run pre-commit install` after `uv tool install pre-commit`.
