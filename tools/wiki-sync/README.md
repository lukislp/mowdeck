# mowdeck-wiki-sync

Generates a flat GitHub wiki mirror from `docs/`. The documentation in `docs/` is the source of truth; the wiki is a generated mirror and is never edited by hand. The `wiki` workflow (`.github/workflows/wiki.yml`) runs this tool on every push to `main` that touches the docs.

```bash
uv run wiki-sync build --docs docs --mkdocs mkdocs.yml --out build/wiki [--commit <sha>]
uv run poe wiki          # same, with the default arguments
```

What it does:

- maps each docs page to a flat wiki page name (`adr/0009-command-and-event-envelopes.md` becomes `ADR-0009-Command-And-Event-Envelopes.md`, `index.md` becomes `Home.md`), see `page_name` in `src/wiki_sync/core.py`;
- skips `docs/plan/**` and `adr/template.md`;
- rewrites relative links between pages to wiki links; links to skipped pages become GitHub blob URLs;
- appends a mirror footer to every page and generates `_Sidebar.md` (from the `mkdocs.yml` nav) and `_Footer.md`;
- is idempotent and deletes stale `*.md` files in the output directory (never `.git`).

Tests: `uv run pytest tools/wiki-sync`.
