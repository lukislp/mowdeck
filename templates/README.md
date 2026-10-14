# templates

Project templates. Currently one: [`service/`](service), the [Copier](https://copier.readthedocs.io) template every MowDeck service is generated from.

```bash
uv run copier copy --trust templates/service services/<service-name>
```

Copier asks for:

| Question | Example | Meaning |
| --- | --- | --- |
| `service_name` | `reports` | kebab-case; distribution name, Compose service, HTTP path `/api/v1/<service_name>` |
| `package_name` | `reports` | Python package (default: `service_name` in snake_case) |
| `description` | `Monthly mowing reports.` | one sentence for README and `pyproject.toml` |
| `needs_db` | `false` | adds SQLAlchemy, Alembic, the migrate step and the database readiness check |
| `http_port` | `8000` | port inside the container (1024 or higher, the image runs as non-root) |
| `permissions` | `{reports.view: View reports}` | YAML mapping key to description; the first one protects the example endpoint |

Non-interactive: `uv run copier copy --trust --defaults -d service_name=reports -d needs_db=true templates/service services/reports`.

The full guide (what you get, the layer rules, the definition of done for a new service) is [docs/dev/service-template.md](../docs/dev/service-template.md). `tools/repo-checks/tests/test_template.py` renders the template in both variants on every test run; the files under `service/template/` are Jinja sources and excluded from ruff, the generated services are linted instead.
