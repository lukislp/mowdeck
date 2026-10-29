"""Architecture rule of ADR 0065: core code never imports a vendor adapter or a vendor SDK.

Vendors plug in only through the entry point groups ``mowdeck.connections`` (connection
factories) and ``mowdeck.cloud_providers`` (provider plugins: credential fields, discovery,
connections). An adapter package is a library under ``libs/`` whose ``pyproject.toml`` has a
``[tool.mowdeck.adapter]`` table naming the vendor modules it wraps (``vendor-modules``). Core is
every other Python source of the services, libraries and integrations: none of it may import an
adapter package or one of those vendor modules, not even inside a function. Tests are exempt
(they may exercise an installed adapter end to end).
"""

import ast
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

ENTRY_POINT_GROUPS = ("mowdeck.connections", "mowdeck.cloud_providers")


@dataclass(frozen=True)
class Adapter:
    """An adapter package: its directory, import name and the vendor modules it wraps."""

    path: Path
    module: str
    vendor_modules: tuple[str, ...]
    entry_points: dict[str, dict[str, str]]


def _pyproject(path: Path) -> dict[str, object]:
    return tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))


def adapters(repo_root: Path) -> list[Adapter]:
    """Every library that declares ``[tool.mowdeck.adapter]``."""
    found: list[Adapter] = []
    for path in sorted((repo_root / "libs").iterdir()):
        if not (path / "pyproject.toml").is_file():
            continue
        data = _pyproject(path)
        tool = data.get("tool", {})
        assert isinstance(tool, dict)
        table = tool.get("mowdeck", {}).get("adapter")
        if table is None:
            continue
        project = data["project"]
        assert isinstance(project, dict)
        found.append(
            Adapter(
                path=path,
                module=str(project["name"]).replace("-", "_"),
                vendor_modules=tuple(table.get("vendor-modules", ())),
                entry_points=dict(project.get("entry-points", {})),
            )
        )
    return found


def core_sources(repo_root: Path, adapter_paths: set[Path]) -> list[Path]:
    """The Python sources of everything that is not an adapter package (tests excluded)."""
    roots = [p / "src" for p in sorted((repo_root / "services").iterdir())]
    roots += [p / "src" for p in sorted((repo_root / "libs").iterdir()) if p not in adapter_paths]
    roots += [p / "src" for p in sorted((repo_root / "integrations").iterdir())]
    return [f for root in roots if root.is_dir() for f in sorted(root.rglob("*.py"))]


def imported_modules(source: str) -> set[str]:
    """Top-level names of every module ``source`` imports, anywhere in the file."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def violations(files: list[Path], forbidden: set[str], repo_root: Path) -> list[str]:
    """``file: module`` for every forbidden import."""
    found: list[str] = []
    for path in files:
        bad = imported_modules(path.read_text(encoding="utf-8")) & forbidden
        found.extend(f"{path.relative_to(repo_root).as_posix()}: {name}" for name in sorted(bad))
    return found


def test_scanner_finds_imports_anywhere(tmp_path: Path) -> None:
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
    assert violations([target], forbidden, tmp_path) == [
        "core.py: acme_sdk",
        "core.py: mowdeck_control_acme",
    ]


def test_there_are_adapter_packages(repo_root: Path) -> None:
    found = adapters(repo_root)
    assert found, "no library declares [tool.mowdeck.adapter]"
    assert all(a.module.startswith("mowdeck_control_") for a in found)


def test_core_never_imports_an_adapter_or_a_vendor_sdk(repo_root: Path) -> None:
    found = adapters(repo_root)
    forbidden = {a.module for a in found} | {m for a in found for m in a.vendor_modules}
    files = core_sources(repo_root, {a.path for a in found})
    assert len(files) > 100  # the scan really covers the services
    problems = violations(files, forbidden, repo_root)
    assert not problems, "core code imports vendor code (ADR 0065):\n" + "\n".join(problems)


@pytest.mark.parametrize("group", ENTRY_POINT_GROUPS)
def test_entry_point_targets_live_in_their_own_package(repo_root: Path, group: str) -> None:
    for adapter in adapters(repo_root):
        for name, target in adapter.entry_points.get(group, {}).items():
            module = target.split(":")[0].split(".")[0]
            assert module == adapter.module, f"{adapter.path.name}: {group} {name} -> {target}"


def test_adapters_plug_in_through_entry_points_only(repo_root: Path) -> None:
    found = adapters(repo_root)
    for adapter in found:
        groups = set(adapter.entry_points) & set(ENTRY_POINT_GROUPS)
        assert groups, f"{adapter.path.name} registers no mowdeck entry point"
        assert adapter.vendor_modules, f"{adapter.path.name} names no vendor-modules"
        others = {a.module for a in found if a is not adapter}
        sources = sorted((adapter.path / "src").rglob("*.py"))
        problems = violations(sources, others, repo_root)
        assert not problems, "an adapter imports another adapter:\n" + "\n".join(problems)
