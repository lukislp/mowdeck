"""The service template renders a complete service skeleton.

The generated tests are not run here (that would need a workspace sync); the Device-Gateway is
generated from the template and is linted, type checked and tested with the rest of the repo.
"""

import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

copier = pytest.importorskip("copier")

COMMON_FILES = (
    ".copier-answers.yml",
    "README.md",
    "pyproject.toml",
    "Dockerfile",
    "Dockerfile.dockerignore",
    "compose.snippet.yml",
    "src/{pkg}/__init__.py",
    "src/{pkg}/py.typed",
    "src/{pkg}/main.py",
    "src/{pkg}/settings.py",
    "src/{pkg}/module.py",
    "src/{pkg}/domain/__init__.py",
    "src/{pkg}/application/__init__.py",
    "src/{pkg}/application/ports.py",
    "src/{pkg}/adapters/api/routes.py",
    "src/{pkg}/adapters/bus/subscriptions.py",
    "tests/test_{pkg}_api.py",
    "tests/test_{pkg}_app.py",
)
DB_FILES = (
    "alembic.ini",
    "src/{pkg}/adapters/db/__init__.py",
    "src/{pkg}/adapters/db/migrate.py",
    "src/{pkg}/adapters/db/tables.py",
    "src/{pkg}/adapters/db/migrations/env.py",
    "src/{pkg}/adapters/db/migrations/script.py.mako",
    "src/{pkg}/adapters/db/migrations/versions/0001_initial.py",
)


def _render(repo_root: Path, destination: Path, **answers: Any) -> Path:
    copier.run_copy(
        str(repo_root / "templates" / "service"),
        str(destination),
        data=answers,
        defaults=True,
        unsafe=True,
        quiet=True,
    )
    return destination


def _files(paths: tuple[str, ...], package: str) -> list[str]:
    return [p.format(pkg=package) for p in paths]


def test_template_renders_a_service_without_database(repo_root: Path, tmp_path: Path) -> None:
    service = _render(repo_root, tmp_path / "reports", service_name="reports")
    for relative in _files(COMMON_FILES, "reports"):
        assert (service / relative).is_file(), f"missing {relative}"
    for relative in _files(DB_FILES, "reports"):
        assert not (service / relative).exists(), f"unexpected {relative}"

    project = tomllib.loads((service / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["name"] == "reports"
    assert project["license"] == "AGPL-3.0-only"
    assert project["requires-python"] == ">=3.13,<3.14"
    assert "mowdeck-service-kit" in project["dependencies"]
    assert project["scripts"] == {"reports": "reports.main:main"}

    module = (service / "src/reports/module.py").read_text("utf-8")
    assert 'VIEW_PERMISSION = "reports.view"' in module
    routes = (service / "src/reports/adapters/api/routes.py").read_text("utf-8")
    assert 'prefix="/reports"' in routes
    assert "require_permission(VIEW_PERMISSION)" in routes

    dockerfile = (service / "Dockerfile").read_text("utf-8")
    assert "uv sync --frozen --no-dev --no-editable --package reports" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert 'CMD ["python", "-m", "reports.main"]' in dockerfile
    assert "/healthz" in dockerfile

    snippet = yaml.safe_load((service / "compose.snippet.yml").read_text("utf-8"))
    block = snippet["services"]["reports"]
    assert block["read_only"] is True
    assert "traefik.http.routers.reports.rule=PathPrefix(`/api/v1/reports`)" in block["labels"]
    # Kubernetes: the umbrella chart infra/helm/mowdeck, not per-service Helm values.
    assert not (service / "helm").exists()


def test_template_renders_database_wiring_and_permissions(repo_root: Path, tmp_path: Path) -> None:
    service = _render(
        repo_root,
        tmp_path / "maintenance-planner",
        service_name="maintenance-planner",
        needs_db=True,
        http_port=8100,
        permissions={"maintenance.view": "View plans", "maintenance.edit": "Edit plans"},
    )
    package = "maintenance_planner"
    for relative in _files(COMMON_FILES + DB_FILES, package):
        assert (service / relative).is_file(), f"missing {relative}"

    project = tomllib.loads((service / "pyproject.toml").read_text("utf-8"))["project"]
    assert "mowdeck-service-kit[db]" in project["dependencies"]
    assert any(dep.startswith("alembic") for dep in project["dependencies"])
    assert project["scripts"]["maintenance-planner-migrate"] == (
        "maintenance_planner.adapters.db.migrate:main"
    )

    module = (service / f"src/{package}/module.py").read_text("utf-8")
    assert 'VIEW_PERMISSION = "maintenance.view"' in module
    assert 'PermissionDef(key="maintenance.edit", description="Edit plans")' in module
    assert "EXPOSE 8100" in (service / "Dockerfile").read_text("utf-8")

    snippet = yaml.safe_load((service / "compose.snippet.yml").read_text("utf-8"))
    migrate = snippet["services"]["maintenance-planner-migrate"]
    assert migrate["command"] == ["python", "-m", f"{package}.adapters.db.migrate"]
    depends = snippet["services"]["maintenance-planner"]["depends_on"]
    assert depends["maintenance-planner-migrate"] == {"condition": "service_completed_successfully"}


def test_template_rejects_invalid_service_names(repo_root: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="kebab-case"):
        _render(repo_root, tmp_path / "bad", service_name="Bad_Name")


def test_no_template_source_is_named_like_a_dockerfile(repo_root: Path) -> None:
    # OpenSSF Scorecard parses every file whose name contains "dockerfile" (in any case) as a
    # Dockerfile; an unrendered Jinja source makes its Pinned-Dependencies check fail with a parse
    # error. The sources use the computed Copier value image_build_file instead
    # (templates/service/copier.yml).
    template = repo_root / "templates" / "service" / "template"
    offenders = [
        p.relative_to(template).as_posix()
        for p in template.rglob("*")
        if "dockerfile" in p.name.lower()
    ]
    assert offenders == []
