# Contributing

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
- Never tag by hand. The release tag is SSH-signed, images, chart and release files are signed with Sigstore and carry build provenance; how to verify them is in [Releases](docs/ops/release.md#verifying-a-release).

## Definition of done

A change is done when:

1. Tests exist and are green (including tenant-isolation tests where data is touched).
2. Lint is clean (ruff, ruff format, mypy strict; for the frontend ESLint, Prettier, Stylelint, svelte-check and the i18n check).
3. Documentation is updated (affected README, `docs/`), and an ADR is added or updated for every architecture decision.
4. `mkdocs build --strict` passes.

A new service is generated from `templates/service` and has its own definition of done in `docs/dev/service-template.md`.

A new third-party image (a base image, an infrastructure container, an image only CI runs) is pinned under "Image sources" in `infra/compose/.env.example` and used through its `*_IMAGE` variable or build argument; `tools/repo-checks/tests/test_images.py` keeps every default equal to that list. CI pulls the list through the repository's GHCR mirror: the workflow `mirror-images.yml` copies a new or bumped image there when the pull request changes the list (re-run CI jobs that pulled before the copy finished). See `infra/compose/README.md`, "Image sources".

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

`pnpm.overrides` in the root `package.json` lifts transitive dependencies with a known advisory whose parent has no fixed release yet, each one scoped to that parent: `xcode>uuid` (`@capacitor/cli`, GHSA-w5hq-g745-h8pq; uuid 11 still ships the CommonJS build xcode requires). Drop an entry once the parent depends on a fixed version itself, then run `pnpm install` and commit the lock file.

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
- [ ] Keyboard focus visible, `prefers-reduced-motion` respected Optional git hooks: `uv run pre-commit install` after `uv tool install pre-commit`.
