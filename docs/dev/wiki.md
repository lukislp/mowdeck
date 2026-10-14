# GitHub wiki mirror

The GitHub wiki of the repository is a generated, read-only mirror of `docs/`. `docs/` is the single source of truth: **edit pages in `docs/`, never in the wiki.** Any manual wiki edit is overwritten by the next sync.

## How it works

The `wiki` workflow (`.github/workflows/wiki.yml`) runs on every push to `main` that touches `docs/**`, `mkdocs.yml`, `tools/wiki-sync/**` or the workflow itself, and on manual dispatch. It runs `wiki-sync build`, clones `<repo>.wiki.git`, replaces its contents with the generated pages and pushes a commit `docs: sync wiki from <sha>` (only when something changed).

The tool lives in `tools/wiki-sync/`. It reads the nav from `mkdocs.yml`, writes flat pages, rewrites relative links between pages to wiki links, generates `_Sidebar.md` and `_Footer.md`, appends a mirror note to each page and deletes stale pages.

## Naming rules

The wiki has no folders, so the path becomes the page name:

| Docs path | Wiki page |
| --- | --- |
| `index.md` | `Home` |
| `phases.md` | `Phases` |
| `architecture/05-building-blocks.md` | `Architecture-05-Building-Blocks` |
| `adr/0009-command-and-event-envelopes.md` | `ADR-0009-Command-And-Event-Envelopes` |
| `adr/README.md` | `ADR-Index` |
| `dev/getting-started.md` | `Dev-Getting-Started` |
| `ops/compose.md` | `Ops-Compose` |
| `spikes/index.md` | `Spikes` |
| `spikes/0001-ble-bridge-and-pymammotion.md` | `Spike-0001-Ble-Bridge-And-Pymammotion` |
| `guides/index.md` | `Guides` |

Directory prefixes: `architecture` to `Architecture`, `adr` to `ADR`, `dev` to `Dev`, `ops` to `Ops`, `spikes` to `Spike`, `guides` to `Guide`. The file stem is title-cased per hyphen segment and digits are kept. `docs/plan/**` (the German original) and `adr/template.md` are not mirrored; links to them point to the file on GitHub.

## One-time setup (repository owner)

GitHub creates the wiki repository only when the first page is created in the web UI. Open `https://github.com/lukislp/mowdeck/wiki`, click "Create the first page" and save any content. Until then the workflow fails with the message "Wiki repository not initialised". After creating the page, re-run the workflow from the Actions tab; the generated pages replace the placeholder.

## Running locally

```bash
uv run poe wiki
```

This writes the pages to `build/wiki` (ignored by git). To pass a commit hash for the footer, call the tool directly: `uv run wiki-sync build --docs docs --mkdocs mkdocs.yml --out build/wiki --commit <sha>`.
