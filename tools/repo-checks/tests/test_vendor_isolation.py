"""The core stays manufacturer independent (ADR 0012, 0019, 0065).

Vendor code lives only in the adapter packages ``libs/mowdeck-control-<vendor>``, which plug in
through entry points (``mowdeck.connections``, ``mowdeck.error_lexicons``). These checks fail
when core code imports an adapter package or its vendor library, when a service other than the
Device-Gateway depends on one, or when an adapter package ships no error lexicon.
"""

import ast
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

ADAPTER_PREFIX = "mowdeck-control-"

KNOWN_EXCEPTIONS = {
    # Linked cloud accounts (ADR 0034): the gateway logs in to Mammotion accounts and lists
    # their devices through the adapter package directly. To be moved behind an entry point.
    "services/device-gateway/src/device_gateway/adapters/control/cloud_accounts.py",
}
"""Core files allowed to import an adapter package today (each with its reason)."""

ADAPTER_HOSTS = {"device-gateway"}
"""Services that install adapter packages (and so the vendors' error lexicons)."""


def _adapter_dirs(repo_root: Path) -> list[Path]:
    return sorted(p for p in (repo_root / "libs").glob(f"{ADAPTER_PREFIX}*") if p.is_dir())


def _project(path: Path) -> dict[str, object]:
    project: dict[str, object] = tomllib.loads((path / "pyproject.toml").read_text("utf-8"))[
        "project"
    ]
    return project


def _requirement_name(requirement: str) -> str:
    return re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0].strip().lower()


def vendor_modules(repo_root: Path) -> set[str]:
    """Top-level modules of the adapter packages and of the vendor libraries they depend on."""
    modules: set[str] = set()
    for package in _adapter_dirs(repo_root):
        modules.add(package.name.replace("-", "_"))
        dependencies = _project(package).get("dependencies", [])
        assert isinstance(dependencies, list)
        for requirement in dependencies:
            name = _requirement_name(str(requirement))
            if not name.startswith("mowdeck-") and name not in {"packaging"}:
                modules.add(name.replace("-", "_"))
    return modules


def core_sources(repo_root: Path) -> Iterator[Path]:
    """Python files of the core: every service and every library except the adapters."""
    adapters = set(_adapter_dirs(repo_root))
    for group in ("services", "libs"):
        for package in sorted((repo_root / group).iterdir()):
            if not package.is_dir() or package in adapters:
                continue
            for source in sorted(package.rglob("*.py")):
                parts = set(source.relative_to(package).parts)
                if parts & {"tests", ".venv", "node_modules", "__pycache__"}:
                    continue
                yield source


def imported_modules(source: Path) -> set[str]:
    """Top-level module names a file imports (absolute imports only)."""
    tree = ast.parse(source.read_text("utf-8"), filename=str(source))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".", 1)[0])
    return names


def test_vendor_isolation_finds_the_mammotion_adapter(repo_root: Path) -> None:
    modules = vendor_modules(repo_root)
    assert {"mowdeck_control_mammotion", "pymammotion"} <= modules
    assert "mowdeck_control" not in modules and "mowdeck_contracts" not in modules


def test_vendor_isolation_core_imports_no_vendor_package(repo_root: Path) -> None:
    vendors = vendor_modules(repo_root)
    offenders = []
    for source in core_sources(repo_root):
        relative = source.relative_to(repo_root).as_posix()
        hits = imported_modules(source) & vendors
        if hits and relative not in KNOWN_EXCEPTIONS:
            offenders.append(f"{relative}: {', '.join(sorted(hits))}")
    assert not offenders, (
        "core code imports a vendor package; plug it in through an entry point "
        "(mowdeck.connections, mowdeck.error_lexicons) instead:\n" + "\n".join(offenders)
    )


def test_vendor_isolation_exceptions_are_still_needed(repo_root: Path) -> None:
    vendors = vendor_modules(repo_root)
    for relative in KNOWN_EXCEPTIONS:
        assert imported_modules(repo_root / relative) & vendors, f"{relative}: drop the exception"


def test_vendor_isolation_only_the_gateway_depends_on_adapters(repo_root: Path) -> None:
    adapters = {p.name for p in _adapter_dirs(repo_root)}
    for service in sorted((repo_root / "services").iterdir()):
        if not (service / "pyproject.toml").is_file():
            continue
        dependencies = _project(service).get("dependencies", [])
        assert isinstance(dependencies, list)
        names = {_requirement_name(str(r)) for r in dependencies}
        if service.name not in ADAPTER_HOSTS:
            assert not names & adapters, f"{service.name} depends on {names & adapters}"


@pytest.mark.parametrize(
    "group", ["mowdeck.connections", "mowdeck.error_lexicons"], ids=["connections", "lexicons"]
)
def test_vendor_isolation_adapters_register_connections_and_a_lexicon(
    repo_root: Path, group: str
) -> None:
    for package in _adapter_dirs(repo_root):
        entry_points = _project(package).get("entry-points", {})
        assert isinstance(entry_points, dict)
        assert entry_points.get(group), f"{package.name} registers nothing under {group}"


def test_vendor_isolation_core_ships_no_vendor_code_table(repo_root: Path) -> None:
    """The web app and the core services hold no vendor error texts: a string of the Mammotion
    lexicon (its curated title of code 1421) appears only in the adapter package."""
    marker = "Auf dem Weg zur Ladestation festgefahren"
    roots = [repo_root / "apps" / "web" / "src", repo_root / "services"]
    for root in roots:
        for source in root.rglob("*"):
            if source.suffix not in {".py", ".ts", ".svelte", ".json"} or "tests" in source.parts:
                continue
            if "node_modules" in source.parts:
                continue
            assert marker not in source.read_text("utf-8", errors="ignore"), source
