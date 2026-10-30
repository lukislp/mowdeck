"""What the installed distribution declares: entry points, ``pyproject.toml``, sources.

Nothing here imports a vendor module or a provider package by name; the kit learns them from
the distribution under test.
"""

import ast
import tomllib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from importlib.metadata import Distribution, EntryPoint, PackageNotFoundError, distribution
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Final

ADAPTER_PREFIX: Final = "mowdeck-control-"
"""Adapter packages are named ``mowdeck-control-<vendor>`` (ADR 0066); the repository's vendor
isolation test finds them by this prefix."""

CONNECTIONS: Final = "mowdeck.connections"
CLOUD_PROVIDERS: Final = "mowdeck.cloud_providers"
ERROR_LEXICONS: Final = "mowdeck.error_lexicons"
GROUPS: Final = (CONNECTIONS, CLOUD_PROVIDERS, ERROR_LEXICONS)
LINK_GROUPS: Final = (CONNECTIONS, CLOUD_PROVIDERS)
"""An adapter registers at least one of these: something that reaches mowers."""


@dataclass(frozen=True)
class InstalledPackage:
    """One installed provider distribution and what it declares."""

    name: str
    module: str
    dist: Distribution

    def entry_points(self, group: str) -> list[EntryPoint]:
        """The distribution's own entry points of ``group``, sorted by name."""
        return sorted(
            (ep for ep in self.dist.entry_points if ep.group == group), key=lambda ep: ep.name
        )

    def source(self, group: str) -> Callable[[], Iterable[EntryPoint]]:
        """An entry point source for the loaders of ``mowdeck_control`` that yields only this
        distribution's entry points of ``group`` (other installed adapters stay out)."""
        return lambda: self.entry_points(group)

    def package_dir(self) -> Path | None:
        """The directory of the top-level package, None when it cannot be found."""
        spec = find_spec(self.module)
        if spec is None or not spec.submodule_search_locations:
            return None
        return Path(next(iter(spec.submodule_search_locations)))

    def pyproject(self) -> dict[str, Any] | None:
        """The distribution's ``pyproject.toml`` when it is installed from a source tree
        (editable, as in the workspace); None for an installed wheel."""
        package = self.package_dir()
        if package is None:
            return None
        for parent in package.parents:
            candidate = parent / "pyproject.toml"
            if candidate.is_file():
                data = tomllib.loads(candidate.read_text("utf-8"))
                if data.get("project", {}).get("name") == self.name:
                    return data
                return None
        return None

    def adapter_table(self) -> dict[str, Any] | None:
        """``[tool.mowdeck.adapter]`` of the pyproject, None without a pyproject."""
        data = self.pyproject()
        if data is None:
            return None
        table: dict[str, Any] = data.get("tool", {}).get("mowdeck", {}).get("adapter", {})
        return table

    def sources(self) -> Iterator[Path]:
        """Every Python file of the package."""
        package = self.package_dir()
        if package is None:
            return
        yield from (p for p in sorted(package.rglob("*.py")) if "__pycache__" not in p.parts)


def installed(name: str) -> InstalledPackage:
    """The installed distribution ``name``; raises ``PackageNotFoundError``."""
    dist = distribution(name)
    return InstalledPackage(name=name, module=name.replace("-", "_"), dist=dist)


def is_installed(name: str) -> bool:
    """True when the distribution ``name`` is installed."""
    try:
        distribution(name)
    except PackageNotFoundError:
        return False
    return True


def target_module(entry_point: EntryPoint) -> str:
    """The top-level module an entry point points into (``pkg.mod:attr`` gives ``pkg``)."""
    return entry_point.value.split(":", 1)[0].split(".", 1)[0].strip()


def imported_modules(source: str) -> set[str]:
    """Top-level module names imported anywhere in ``source`` (absolute imports)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".", 1)[0])
    return names


def foreign_adapter_imports(package: InstalledPackage) -> list[str]:
    """``file: module`` for every import of another adapter package (``mowdeck_control_*``)."""
    own = package.module
    prefix = ADAPTER_PREFIX.replace("-", "_")
    found: list[str] = []
    for path in package.sources():
        for module in sorted(imported_modules(path.read_text("utf-8"))):
            if module.startswith(prefix) and module != own:
                found.append(f"{path.name}: {module}")
    return found


SDK_FREE_IMPORT: Final = """
import importlib, json, sys
from importlib.abc import MetaPathFinder

blocked = json.loads(sys.argv[1])

class Block(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == m or name.startswith(m + ".") for m in blocked):
            raise ImportError("blocked vendor module: " + name)
        return None

sys.meta_path.insert(0, Block())
for target in json.loads(sys.argv[2]):
    module, _, attr = target.partition(":")
    value = importlib.import_module(module.strip())
    for part in attr.strip().split(".") if attr.strip() else ():
        value = getattr(value, part)
loaded = sorted(m for m in sys.modules if any(m == b or m.startswith(b + ".") for b in blocked))
print(json.dumps(loaded))
"""
"""Run in a fresh interpreter: import entry point targets while the vendor modules are blocked."""
