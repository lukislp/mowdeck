"""Optional add-ons stay add-ons (ADR 0075): the core never depends on them.

The assistant (chat control) is an optional service. These checks fail when

* a core service, library or integration imports the add-on's package, depends on it, or names
  its API (``/api/v1/assistant``) anywhere in its sources;
* the add-on imports another service's package, the authorization library (it must not check
  or write OpenFGA itself) or the bus (it has no NATS user), or depends on anything beyond the
  public-API client libraries;
* the add-on is not optional in Compose and Helm, has a NATS user, a database or OpenFGA
  access, or a core service waits for it.

So removing ``services/assistant`` (and its Compose, Helm and web entries) leaves everything else
working: no core code path reaches it.
"""

import ast
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

ADDONS = {"assistant": "assistant"}
"""Add-on service directory to its Python package."""

ADDON_DEPENDENCIES = {"httpx", "mowdeck-auth", "mowdeck-contracts", "mowdeck-service-kit"}
"""What the add-on may depend on: an HTTP client and the shared contracts, kit and token check."""

FORBIDDEN_IN_ADDON = ("mowdeck_authz", "mowdeck_service_kit.bus", "nats", "sqlalchemy")
"""Modules the add-on must not import (OpenFGA, the bus, a database)."""


def _python_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*.py"):
        if "__pycache__" not in path.parts:
            yield path


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _core_roots(repo_root: Path) -> list[Path]:
    roots = [
        p / "src" for p in (repo_root / "services").iterdir() if p.is_dir() and p.name not in ADDONS
    ]
    roots += [p / "src" for p in (repo_root / "libs").iterdir() if p.is_dir()]
    roots += [p / "src" for p in (repo_root / "integrations").iterdir() if p.is_dir()]
    return [r for r in roots if r.is_dir()]


def _service_packages(repo_root: Path) -> set[str]:
    packages: set[str] = set()
    for service in (repo_root / "services").iterdir():
        src = service / "src"
        if service.name in ADDONS or not src.is_dir():
            continue
        packages.update(p.name for p in src.iterdir() if (p / "__init__.py").is_file())
    return packages


def test_the_core_does_not_import_or_name_an_addon(repo_root: Path) -> None:
    problems: list[str] = []
    for root in _core_roots(repo_root):
        for path in _python_files(root):
            rel = path.relative_to(repo_root).as_posix()
            for module in _imports(path):
                if module.split(".")[0] in ADDONS.values():
                    problems.append(f"{rel} imports {module}")
            if "/api/v1/assistant" in path.read_text("utf-8"):
                problems.append(f"{rel} names the assistant's API")
    assert not problems, "the core depends on an optional add-on:\n" + "\n".join(problems)


def test_no_core_package_depends_on_an_addon(repo_root: Path) -> None:
    for kind in ("services", "libs", "integrations"):
        for project in (repo_root / kind).glob("*/pyproject.toml"):
            if project.parent.name in ADDONS:
                continue
            data = tomllib.loads(project.read_text("utf-8"))
            deps = data.get("project", {}).get("dependencies", [])
            names = {re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0] for d in deps}
            assert not names & set(ADDONS), f"{project} depends on an add-on"


def test_the_addon_talks_to_the_rest_only_through_the_public_api(repo_root: Path) -> None:
    services = _service_packages(repo_root)
    for directory, package in ADDONS.items():
        src = repo_root / "services" / directory / "src" / package
        for path in _python_files(src):
            rel = path.relative_to(repo_root).as_posix()
            for module in _imports(path):
                top = module.split(".")[0]
                assert top not in services, f"{rel} imports the service package {module}"
                assert not any(
                    module == f or module.startswith(f + ".") for f in FORBIDDEN_IN_ADDON
                ), f"{rel} imports {module}: the add-on has no OpenFGA, bus or database access"
        project = tomllib.loads(
            (repo_root / "services" / directory / "pyproject.toml").read_text("utf-8")
        )
        deps = {
            re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0]
            for d in project["project"].get("dependencies", [])
        }
        assert deps <= ADDON_DEPENDENCIES, (
            f"{directory} depends on {sorted(deps - ADDON_DEPENDENCIES)}"
        )


def _compose(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "infra" / "compose" / "docker-compose.yml"
    data: dict[str, Any] = yaml.safe_load(path.read_text("utf-8"))
    services: dict[str, Any] = data["services"]
    return services


def test_the_addon_is_optional_and_unprivileged_in_compose(repo_root: Path) -> None:
    services = _compose(repo_root)
    for name in ADDONS:
        service = services[name]
        assert service.get("profiles") == [name], f"{name} must be its own Compose profile"
        env = service.get("environment", {})
        assert env.get("MOWDECK_NATS_ENABLED") == "false", f"{name} must not connect to NATS"
        for privileged in ("MOWDECK_NATS_URL", "MOWDECK_DATABASE_URL", "MOWDECK_OPENFGA_API_URL"):
            assert privileged not in env, f"{name} must not get {privileged}"
        for other, spec in services.items():
            depends = spec.get("depends_on", {})
            assert name not in depends, f"{other} must not wait for the optional {name}"
        identity = services["identity"]["environment"]["MOWDECK_IDENTITY_MODULE_URLS"]
        assert f"http://{name}:" not in identity.split("${")[0], (
            "Identity lists the add-on only through IDENTITY_EXTRA_MODULE_URLS"
        )


def test_the_addon_is_off_by_default_in_helm_and_has_no_nats_user(repo_root: Path) -> None:
    chart = repo_root / "infra" / "helm" / "mowdeck"
    values = yaml.safe_load((chart / "values.yaml").read_text("utf-8"))
    nats = (repo_root / "tools" / "repo-checks" / "scripts" / "nats_auth.py").read_text("utf-8")
    for name in ADDONS:
        assert values["services"][name]["enabled"] is False
        assert f"NATS_PASSWORD_{name.upper()}" not in values["secrets"]
        assert not re.search(rf'^\s*"{name}"\s*:', nats, re.MULTILINE), (
            f"{name} must not be a NATS user"
        )
        template = (chart / "templates" / f"{name}.yaml").read_text("utf-8")
        assert '"MOWDECK_NATS_ENABLED" "false"' in template
        assert "authz" not in template.split("mowdeck.workload", 1)[1]
