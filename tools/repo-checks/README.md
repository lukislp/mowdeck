# mowdeck-repo-checks

Pytest tests that guard repository conventions. They run as part of `uv run poe test`.

| Check | What it guards |
| --- | --- |
| compose | `infra/compose/docker-compose.yml` parses as YAML and `docker compose config -q` passes (skipped if Docker is missing) |
| ADRs | every file in `docs/adr/` named `NNNN-*.md` has Status, Context, Decision, Consequences headings |
| packages | every directory under `services/` and `libs/` has `README.md` and `pyproject.toml` |
| docs | `mkdocs build --strict` succeeds (skipped if mkdocs is not importable) |
| images | every image default in the Dockerfiles (`ARG *_IMAGE`, `FROM`), Compose (`${*_IMAGE:-...}`, snippets, `.env.example`) and the CI mirror references comes from `infra/images.txt` |

Run on its own: `uv run pytest tools/repo-checks`.
