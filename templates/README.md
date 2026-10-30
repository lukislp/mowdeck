# templates

Project templates for [Copier](https://copier.readthedocs.io): [`service/`](service), the template every MowDeck service is generated from, and [`provider/`](provider), the template of a vendor adapter package.

## service

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

The full guide (what you get, the layer rules, the definition of done for a new service) is [docs/dev/service-template.md](../docs/dev/service-template.md). `tools/repo-checks/tests/test_template.py` renders the template in both variants on every test run; the files under `service/template/` are Jinja sources and excluded from ruff, the generated services are linted instead. The Dockerfile sources are named `{{ image_build_file }}.jinja` and `{{ image_build_file }}.dockerignore.jinja` (a computed Copier value that renders to `Dockerfile`): tools that parse every file whose name contains "dockerfile" as a Dockerfile, such as the OpenSSF Scorecard, would otherwise try to parse the Jinja source.

## provider

A new mower vendor as a package `libs/mowdeck-control-<vendor>` ([ADR 0069](../docs/adr/0069-provider-conformance-kit-and-package-template.md)): a cloud provider plugin, a minimal connection (start, pause, resume, dock, emergency stop; nothing else declared), the port to the vendor cloud with a fake client, an empty error lexicon, English and German texts, a README, and the provider conformance kit already wired into its tests, which run green as generated.

```bash
uv run poe new-provider <vendor>
# the same as
uv run copier copy --trust -d vendor=<vendor> templates/provider libs/mowdeck-control-<vendor>
```

| Question | Example | Meaning |
| --- | --- | --- |
| `vendor` | `acme` | lower-case letters and digits; package `mowdeck-control-<vendor>`, provider id, lexicon key |
| `vendor_name` | `Acme Robotics` | the brand users know, shown on the link form |
| `sdk_module` | `acme_cloud` | import name of the vendor SDK, declared in `[tool.mowdeck.adapter] vendor-modules` |
| `sdk_distribution` | `acme-cloud` | its PyPI name (left commented out until the real client uses it) |
| `sdk_licence` | `permissive` or `gpl` | **no default**: the SDK's licence from the `LICENSE` file in its sdist; `gpl` makes the package GPL-3.0-only ([ADR 0005](../docs/adr/0005-licensing.md), [ADR 0011](../docs/adr/0011-gpl-package-for-pymammotion-adapter.md)), `permissive` keeps it Apache-2.0 |
| `brand_names` | `[roadrunner]` | further words that name the vendor's products (the core must not use them) |

`--trust` is needed for one task: it copies the licence text the answer asks for from `LICENSES/` into the package's `LICENSE`. The step-by-step guide is [docs/dev/adding-a-provider.md](../docs/dev/adding-a-provider.md). `tools/repo-checks/tests/test_provider_template.py` renders the template (Apache and GPL), makes the generated package importable without installing it, runs its tests (the conformance kit included) and lints and type checks it (ruff, mypy strict); the files under `provider/template/` are excluded from ruff like the service template's.
