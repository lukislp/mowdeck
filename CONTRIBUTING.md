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

`pnpm -r test` includes the Playwright suite of `tests/e2e-web`, which needs the running Compose stack; without it, run `pnpm -r --filter '!e2e-web' test`. After changing a service route or a contract, regenerate the API types: `uv run poe openapi` (checked by `uv run poe openapi-check` and `uv run poe test`), then `pnpm codegen`, and commit the results with the change. Details: [docs/dev/web-app.md](docs/dev/web-app.md).

### UI pull requests

A pull request that touches `apps/web` or `libs/mowdeck-ui` attaches screenshots in light and dark theme at phone (390 x 844) and desktop (1280 x 800) width: with the stack up, `pnpm --filter e2e-web screens` writes the main pages (login, mower list, mower detail in light and dark, mower settings) to `apps/web/screenshots/`; add the changed pages that it does not cover. Its description carries this checklist:

- [ ] Only tokens used; no new colours, radii or fonts outside `tokens.css`
- [ ] Colour is used only for state; every state also has text or a glyph
- [ ] Readouts in Plex Mono with units; labels uppercase 12 px
- [ ] Targets at least 48 px, gaps at least 8 px; checked at 390 px width
- [ ] Emergency stop visible and reachable on every mower view, including with sheets and keyboard open
- [ ] Light and dark screenshots attached; sunlight check (light theme, full brightness) for new status UI
- [ ] No hard-coded strings; German and English texts present and fitting (German is longer)
- [ ] Pending, error, empty, offline and no-permission states designed
- [ ] Keyboard focus visible, `prefers-reduced-motion` respected Optional git hooks: `uv run pre-commit install` after `uv tool install pre-commit`.
