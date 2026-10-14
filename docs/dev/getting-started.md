# Getting started

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/) 0.5 or newer
- Docker with Compose v2
- Git

## Linux and macOS

```bash
git clone <repo-url> mowdeck && cd mowdeck
uv sync
uv run poe lint
uv run poe test
uv run poe docs
cp infra/compose/.env.example infra/compose/.env   # set the passwords
docker compose -f infra/compose/docker-compose.yml up -d --wait
```

## Windows

Install Python 3.13, uv, Docker Desktop and Git for Windows. Use PowerShell or Git Bash.

```powershell
git clone <repo-url> mowdeck; cd mowdeck
uv sync
uv run poe lint
uv run poe test
uv run poe docs
Copy-Item infra\compose\.env.example infra\compose\.env
docker compose -f infra/compose/docker-compose.yml up -d --wait
```

## Everyday commands

| Command | Purpose |
| --- | --- |
| `uv sync` | install the workspace into `.venv` and update `uv.lock` |
| `uv run poe lint` | ruff check, ruff format check, mypy strict |
| `uv run poe fmt` | auto fix and format |
| `uv run poe test` | pytest with coverage |
| `uv run poe docs` | `mkdocs build --strict` |
| `uv run poe compose-check` | validate the Compose file |
| `uv run mkdocs serve` | live preview of the documentation |

## Repository conventions

Conventional Commits, one task at a time with tests and README, ADR for every architecture decision. See `CONTRIBUTING.md`. The tests in `tools/repo-checks` guard these conventions.
