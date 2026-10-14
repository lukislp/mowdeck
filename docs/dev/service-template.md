# Service template

Every MowDeck service is generated from the [Copier](https://copier.readthedocs.io) template in `templates/service`. The template gives a new service the layers, the operational endpoints, tests, a Dockerfile, a Compose block and a README in the agreed structure, so a new service starts compliant with the operating rules (Twelve-Factor, non-root, read-only file system, health endpoints, graceful shutdown). The Device-Gateway was the first service generated from it.

## Generate a service

```bash
uv run copier copy --trust templates/service services/<service-name>
```

| Question | Default | Meaning |
| --- | --- | --- |
| `service_name` | none | kebab-case, e.g. `reports`; distribution name, Compose service and HTTP path `/api/v1/<service_name>` |
| `package_name` | `service_name` in snake_case | Python package under `src/` |
| `description` | `MowDeck <name> service.` | one sentence |
| `needs_db` | `false` | own PostgreSQL database with SQLAlchemy and Alembic |
| `http_port` | `8000` | port inside the container (1024 or higher) |
| `permissions` | `<package>.view: View <name> data` | YAML mapping of permission key to description; the first one protects the example endpoint |

`--trust` is needed because the template computes derived values; `--defaults` with `-d key=value` answers everything on the command line. Then:

1. Add the service to the root `pyproject.toml`: the distribution name to the `dev` dependency group and `<name> = { workspace = true }` to `[tool.uv.sources]`, so `uv sync` installs it editable for tests and mypy.
2. `uv sync`, `uv run poe lint`, `uv run pytest services/<name>`.
3. Paste `compose.snippet.yml` into `infra/compose/docker-compose.yml` (for a database service also add the database and role to `infra/compose/init/01-databases.sql`).

## What you get

```text
services/<name>/
  pyproject.toml           workspace member, AGPL-3.0-only, depends on mowdeck-service-kit and mowdeck-contracts
  README.md                purpose, interfaces, events, settings, local start, tests
  Dockerfile               multi-stage uv build, non-root, read-only friendly, HEALTHCHECK
  Dockerfile.dockerignore  allow list for the repository root build context
  compose.snippet.yml      Compose block with Traefik labels for /api/v1/<name>
  alembic.ini              (needs_db) for creating revisions during development
  src/<package>/
    main.py                build_app(settings, bus=..., principal_resolver=...) and main()
    settings.py            Settings(ServiceSettings) with the service defaults
    module.py              ModuleDescriptor with the declared permissions
    domain/                models and rules, no I/O
    application/           use cases and ports (Protocols)
    adapters/api/          FastAPI routers; GET /api/v1/<name>/ping requires the first permission
    adapters/bus/          subscriptions and publishers
    adapters/db/           (needs_db) tables, migrate entry point, Alembic migrations
  tests/
    test_<package>_api.py  health, module descriptor, 401, 403, 200
    test_<package>_app.py  startup and shutdown with the in-memory bus
```

## Layer rules

| Layer | May import | Must not |
| --- | --- | --- |
| `domain` | standard library, `mowdeck_contracts` | do I/O, import FastAPI, NATS, SQLAlchemy or adapters |
| `application` | `domain`, its own `ports`, libraries without I/O (`mowdeck_control`) | import adapters or `main`; it talks to the outside only through ports |
| `adapters` | `application`, `domain`, frameworks | contain business rules; they translate and implement ports |
| `main` | everything | contain logic beyond wiring |

Ports are `typing.Protocol` classes in `application/ports.py`. Tests replace adapters with fakes through `build_app` arguments, never by patching modules.

## Conventions baked in

- **Configuration** only from `MOWDECK_*` variables or `MOWDECK_<NAME>_FILE` secret files (`Settings.from_env()`); every field has a docstring, which is the settings reference.
- **Operations**: `/healthz`, `/readyz`, `/metrics` and `/.well-known/mowdeck-module` from the kit; JSON logs on stdout; graceful shutdown hooks within `MOWDECK_SHUTDOWN_GRACE_SECONDS`.
- **Image**: built from the repository root with `uv sync --frozen --no-dev --no-editable --package <name>`, so only the service and the workspace libraries it depends on are installed. Runs as user `app` (uid 10001), sets `PYTHONDONTWRITEBYTECODE=1` and writes nothing at runtime: Compose runs it with `read_only: true` and a tmpfs at `/tmp`, Helm with `readOnlyRootFilesystem`. No architecture specific steps, so the same Dockerfile builds amd64 and arm64. The ignore file is `Dockerfile.dockerignore` because the build context is the repository root (BuildKit reads the ignore file next to the Dockerfile).
- **Migrations** (needs_db) run as a separate step before the service: `python -m <package>.adapters.db.migrate` (Compose: one-shot `<name>-migrate` service, Helm: a Job). The Alembic configuration is built in code, so the image needs no `alembic.ini`. Migrations must stay backward compatible for rolling updates.
- **Tests** have unique file names (`test_<package>_*.py`) and no `conftest.py` (mypy treats equally named modules of different packages as duplicates); shared helpers go into a uniquely named module next to the tests.
- **Permissions**: every endpoint uses `require_permission(...)`; endpoints touching account data filter by the principal's account and need a cross-tenant test (see the kit README).

## Definition of done for a new service

1. README in the template structure (purpose, interfaces, events, settings, local start, tests) and kept current.
2. Tests: the generated ones plus tests for every use case; cross-tenant tests for every endpoint and handler touching account data; coverage of at least 85 %.
3. `uv run poe lint` and `uv run poe test` green; the service is listed in the root `pyproject.toml`.
4. Module descriptor declares every permission and every produced event.
5. `compose.snippet.yml` added to `infra/compose/docker-compose.yml`, `uv run poe compose-check` green, and `docker compose up -d --wait` reports the service healthy.
6. A template in the umbrella chart `infra/helm/mowdeck` (`templates/<name>.yaml` calling `mowdeck.workload` and, with a database, `mowdeck.migrationJob`; settings in `values.yaml` under `config`, secrets as `_FILE` files), `sh infra/helm/check.sh` green ([Helm operations](../ops/helm.md)). `tools/repo-checks/tests/test_helm.py` fails until the Compose service has its chart template.
7. Docs: a line in [building blocks](../architecture/05-building-blocks.md), runtime scenarios in [runtime view](../architecture/06-runtime-view.md) where the service takes part, contracts documented in `mowdeck-contracts`.
8. An ADR for every architecture decision the service makes.

## Updating the template

Change the template and the services generated from it together. `tools/repo-checks/tests/test_template.py` renders both variants (with and without database) on every test run; for larger changes also render into `services/`, run lint and the generated tests, and delete the render again. Services keep their answers in `.copier-answers.yml`.
