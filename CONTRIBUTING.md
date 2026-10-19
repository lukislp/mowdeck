# Contributing

## Workflow

- **Conventional Commits** for all commits, in English: `feat(identity): add sub-user creation`, `fix(compose): ...`, `docs(adr): ...`, `chore: ...`. Breaking changes use `!` or a `BREAKING CHANGE:` footer.
- **One task at a time.** Each task delivers its code, tests and a README for what it adds.
- The GitHub wiki is a generated mirror of `docs/`: edit the docs, never the wiki (see `docs/dev/wiki.md`).
- Everything is written in English: code, comments, commits, documentation. The UI is German by default, with English available.

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

`pymammotion` is excluded (`exclude-dependencies: pymammotion`), also inside grouped PRs: a new release changes the protocol surface of the GPL adapter `mowdeck-control-mammotion`, so it is tested on the real mower before it is merged by hand. After that test, approve the PR and enable auto-merge; the workflow then only keeps the branch up to date.

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

The web app (`apps/web`), the design system (`libs/mowdeck-ui`), the type generator (`tools/ts-codegen`) and the Playwright gate (`tests/e2e-web`) form a pnpm workspace. Requirements: Node 22 (`.node-version`) and pnpm 10.34.6 (`corepack enable`). CI runs the same steps in the job `web`:

```bash
pnpm install --frozen-lockfile
pnpm -r lint
pnpm -r check
pnpm -r test
pnpm --filter ts-codegen check
pnpm --filter web build
```

`pnpm -r test` includes the Playwright suite of `tests/e2e-web`, which needs the throwaway test stack (below); without it, run `pnpm -r --filter '!e2e-web' test`. After changing a service route or a contract, regenerate the API types: `uv run poe openapi` (checked by `uv run poe openapi-check` and `uv run poe test`), then `pnpm codegen`, and commit the results with the change. Details: [docs/dev/web-app.md](docs/dev/web-app.md).

`pnpm.overrides` in the root `package.json` lifts transitive dependencies with a known advisory whose parent has no fixed release yet, each one scoped to that parent: `xcode>uuid` (`@capacitor/cli`, GHSA-w5hq-g745-h8pq; uuid 11 still ships the CommonJS build xcode requires) and `@sveltejs/kit>cookie` (GHSA-pxg6-pf52-xh8x; cookie 0.7 keeps the `parse`/`serialize` API). Drop an entry once the parent depends on a fixed version itself, then run `pnpm install` and commit the lock file.

`osv-scanner.toml` at the repository root records advisories accepted on purpose because no fixed release exists; OSV-Scanner and the OpenSSF Scorecard Vulnerabilities check both honour it. It currently holds `braces` 3.0.3 (GHSA-vfj7-8cjw-p6xm), reachable only through the dev-only `stylelint` > `globby`/`fast-glob` > `micromatch` path that expands the repository's own lint globs. Remove an entry as soon as Dependabot or upstream ships a fixed version, and re-check it before its `ignoreUntil` date at the latest instead of just extending it. Check locally with `docker run --rm -v "$PWD:/src" ghcr.io/google/osv-scanner scan source -r /src`.

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
