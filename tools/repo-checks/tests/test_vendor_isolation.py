"""The core stays manufacturer independent (ADR 0012, 0019, 0066, 0067).

Vendor code lives only in the adapter packages ``libs/mowdeck-control-<vendor>``, which plug in
through entry points: ``mowdeck.connections`` (connection factories), ``mowdeck.cloud_providers``
(cloud provider plugins: credential fields, discovery, cloud connections, ADR 0066) and
``mowdeck.error_lexicons`` (error texts, ADR 0067). An adapter package declares the vendor
modules it wraps in ``[tool.mowdeck.adapter] vendor-modules``. These checks fail when core code
(services, the other libraries, integrations; tests exempt) imports an adapter package or a
vendor library anywhere in a file, when a service other than the Device-Gateway depends on one,
when an adapter registers no connection or provider, ships no error lexicon, points an entry
point into another package or imports another adapter.
"""

import ast
import re
import tomllib
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

ADAPTER_PREFIX = "mowdeck-control-"

ADAPTER_HOSTS = {"device-gateway"}
"""Services that install adapter packages (and so the vendors' plugins and error lexicons)."""

ENTRY_POINT_GROUPS = ("mowdeck.connections", "mowdeck.cloud_providers", "mowdeck.error_lexicons")
"""Every group an adapter may register in."""
LINK_GROUPS = ("mowdeck.connections", "mowdeck.cloud_providers")
"""An adapter registers at least one of these: something that reaches mowers."""


def _adapter_dirs(repo_root: Path) -> list[Path]:
    return sorted(p for p in (repo_root / "libs").glob(f"{ADAPTER_PREFIX}*") if p.is_dir())


def _pyproject(path: Path) -> dict[str, object]:
    return tomllib.loads((path / "pyproject.toml").read_text("utf-8"))


def _project(path: Path) -> dict[str, object]:
    project = _pyproject(path)["project"]
    assert isinstance(project, dict)
    return project


def _entry_points(path: Path) -> dict[str, dict[str, str]]:
    entry_points = _project(path).get("entry-points", {})
    assert isinstance(entry_points, dict)
    return entry_points


def _requirement_name(requirement: str) -> str:
    return re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0].strip().lower()


def _declared_vendor_modules(path: Path) -> list[str]:
    """``[tool.mowdeck.adapter] vendor-modules`` of an adapter package."""
    tool = _pyproject(path).get("tool", {})
    assert isinstance(tool, dict)
    table = tool.get("mowdeck", {}).get("adapter", {})
    return [str(m) for m in table.get("vendor-modules", [])]


def _module_of(path: Path) -> str:
    return path.name.replace("-", "_")


def vendor_modules(repo_root: Path) -> set[str]:
    """Top-level modules of the adapter packages and of the vendor libraries they wrap: the
    declared ``vendor-modules`` plus every non-MowDeck dependency."""
    modules: set[str] = set()
    for package in _adapter_dirs(repo_root):
        modules.add(_module_of(package))
        modules.update(_declared_vendor_modules(package))
        dependencies = _project(package).get("dependencies", [])
        assert isinstance(dependencies, list)
        for requirement in dependencies:
            name = _requirement_name(str(requirement))
            if not name.startswith("mowdeck-") and name not in {"packaging"}:
                modules.add(name.replace("-", "_"))
    return modules


def _sources(package: Path) -> Iterator[Path]:
    for source in sorted(package.rglob("*.py")):
        parts = set(source.relative_to(package).parts)
        if parts & {"tests", ".venv", "node_modules", "__pycache__"}:
            continue
        yield source


def core_sources(repo_root: Path) -> Iterator[Path]:
    """Python files of the core: every service, every library except the adapters and every
    integration (tests excluded)."""
    adapters = set(_adapter_dirs(repo_root))
    for group in ("services", "libs", "integrations"):
        base = repo_root / group
        if not base.is_dir():
            continue
        for package in sorted(base.iterdir()):
            if package.is_dir() and package not in adapters:
                yield from _sources(package)


def imported_modules(source: str) -> set[str]:
    """Top-level module names ``source`` imports, anywhere in the file (absolute imports)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".", 1)[0])
    return names


def violations(files: Iterable[Path], forbidden: set[str], repo_root: Path) -> list[str]:
    """``file: modules`` for every file that imports a forbidden module."""
    found: list[str] = []
    for path in files:
        hits = imported_modules(path.read_text("utf-8")) & forbidden
        if hits:
            found.append(f"{path.relative_to(repo_root).as_posix()}: {', '.join(sorted(hits))}")
    return found


def test_vendor_isolation_scanner_finds_imports_anywhere(tmp_path: Path) -> None:
    source = (
        "import os\n"
        "from acme_sdk.client import Client\n"
        "def f():\n"
        "    import mowdeck_control_acme.plugin as plugin\n"
        "from . import sibling\n"
    )
    assert imported_modules(source) == {"os", "acme_sdk", "mowdeck_control_acme"}
    target = tmp_path / "core.py"
    target.write_text(source, encoding="utf-8")
    forbidden = {"acme_sdk", "mowdeck_control_acme"}
    assert violations([target], forbidden, tmp_path) == ["core.py: acme_sdk, mowdeck_control_acme"]


def test_vendor_isolation_finds_the_mammotion_adapter(repo_root: Path) -> None:
    modules = vendor_modules(repo_root)
    assert {"mowdeck_control_mammotion", "pymammotion"} <= modules
    assert "mowdeck_control" not in modules and "mowdeck_contracts" not in modules


def test_vendor_isolation_core_imports_no_vendor_package(repo_root: Path) -> None:
    files = list(core_sources(repo_root))
    assert len(files) > 100  # the scan really covers the services
    offenders = violations(files, vendor_modules(repo_root), repo_root)
    assert not offenders, (
        "core code imports a vendor package; plug it in through an entry point "
        f"({', '.join(ENTRY_POINT_GROUPS)}) instead:\n" + "\n".join(offenders)
    )


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


def test_vendor_isolation_adapters_reach_mowers_and_ship_a_lexicon(repo_root: Path) -> None:
    for package in _adapter_dirs(repo_root):
        entry_points = _entry_points(package)
        assert any(entry_points.get(g) for g in LINK_GROUPS), (
            f"{package.name} registers nothing under {' or '.join(LINK_GROUPS)}"
        )
        assert entry_points.get("mowdeck.error_lexicons"), (
            f"{package.name} registers nothing under mowdeck.error_lexicons"
        )
        assert _declared_vendor_modules(package), (
            f"{package.name}: declare [tool.mowdeck.adapter] vendor-modules"
        )


@pytest.mark.parametrize("group", ENTRY_POINT_GROUPS)
def test_vendor_isolation_entry_points_live_in_their_own_package(
    repo_root: Path, group: str
) -> None:
    for package in _adapter_dirs(repo_root):
        for name, target in _entry_points(package).get(group, {}).items():
            module = target.split(":", 1)[0].split(".", 1)[0]
            assert module == _module_of(package), f"{package.name}: {group} {name} -> {target}"


def test_vendor_isolation_adapters_do_not_import_each_other(repo_root: Path) -> None:
    adapters = _adapter_dirs(repo_root)
    for package in adapters:
        others = {_module_of(p) for p in adapters if p != package}
        problems = violations(_sources(package / "src"), others, repo_root)
        assert not problems, "an adapter imports another adapter:\n" + "\n".join(problems)


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
