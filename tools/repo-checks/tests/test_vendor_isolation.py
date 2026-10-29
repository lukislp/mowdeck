"""The core stays manufacturer independent (ADR 0012, 0019, 0066, 0067).

Vendor code lives only in the adapter packages ``libs/mowdeck-control-<vendor>``, which plug in
through entry points: ``mowdeck.connections`` (connection factories), ``mowdeck.cloud_providers``
(cloud provider plugins: credential fields, mower options, discovery, cloud connections, ADR
0066) and ``mowdeck.error_lexicons`` (error texts, ADR 0067). Each adapter package declares in
``[tool.mowdeck.adapter]`` the vendor modules it wraps (``vendor-modules``) and the brand words
that name its vendor or products (``brand-names``); the vendor words are those plus the
package's own vendor name (``mowdeck-control-<vendor>``).

The core is every Python file under ``src`` of a service, of a library that is no adapter and
of an integration (tests inside ``src`` included), plus the web app's sources (``apps/*/src``,
unit tests excluded) and messages. These checks fail when the core

* imports an adapter package or a declared vendor module, anywhere in a file;
* loads code dynamically (``importlib.import_module``, ``__import__``, ``<entry point>.load()``)
  outside the sanctioned plugin loaders listed in :data:`DYNAMIC_LOADERS`;
* names a vendor in a string literal (docstrings and comments excepted), apart from the few
  entries of :data:`ALLOWED_STRINGS`;
* names a vendor in a web message or in web source code outside comments;

and when a service other than the Device-Gateway depends on an adapter, or an adapter
registers neither a connection nor a provider, ships no error lexicon, declares no vendor
modules, points an entry point into another package or imports another adapter.
"""

import ast
import json
import re
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ADAPTER_PREFIX = "mowdeck-control-"

ADAPTER_HOSTS = {"device-gateway"}
"""Services that install adapter packages (and so the vendors' plugins and error lexicons)."""

ENTRY_POINT_GROUPS = ("mowdeck.connections", "mowdeck.cloud_providers", "mowdeck.error_lexicons")
"""Every group an adapter may register in."""
LINK_GROUPS = ("mowdeck.connections", "mowdeck.cloud_providers")
"""An adapter registers at least one of these: something that reaches mowers."""

DYNAMIC_LOADERS = {
    "libs/mowdeck-control/src/mowdeck_control/registry.py": "connection factories",
    "libs/mowdeck-control/src/mowdeck_control/providers.py": "cloud provider plugins",
    "libs/mowdeck-control/src/mowdeck_control/error_lexicons.py": "error lexicons",
    "libs/mowdeck-patterns/src/mowdeck_patterns/registry.py": "mowing pattern plugins",
    "services/notifications/src/notifications/adapters/channels/__init__.py": "notification "
    "channels",
    "services/vision/src/vision/adapters/providers/llm.py": "the optional LLM client library",
    "integrations/agora-bridge/src/agora_bridge/guard.py": "the operator-built camera SDK guard",
}
"""Core files that may load code dynamically, with what they load (none of it vendor code of
a mower: the vendors come through the three entry point groups)."""

ALLOWED_STRINGS = {
    ("libs/mowdeck-control/src/mowdeck_control/simulator.py", "Luba-SIM0001"): "the simulated "
    "mower carries a device name shaped like a real one, so the device-name paths run in tests",
}
"""String literals of the core that may name a vendor, with the reason."""


@dataclass(frozen=True)
class Adapter:
    """An adapter package and what it declares."""

    path: Path
    module: str
    vendor: str
    vendor_modules: tuple[str, ...]
    brand_names: tuple[str, ...]
    entry_points: dict[str, dict[str, str]]


def adapters(repo_root: Path) -> list[Adapter]:
    """Every ``libs/mowdeck-control-<vendor>`` package."""
    found: list[Adapter] = []
    for path in sorted((repo_root / "libs").glob(f"{ADAPTER_PREFIX}*")):
        if not (path / "pyproject.toml").is_file():
            continue
        data = tomllib.loads((path / "pyproject.toml").read_text("utf-8"))
        project = data["project"]
        table = data.get("tool", {}).get("mowdeck", {}).get("adapter", {})
        found.append(
            Adapter(
                path=path,
                module=path.name.replace("-", "_"),
                vendor=path.name.removeprefix(ADAPTER_PREFIX),
                vendor_modules=tuple(str(m) for m in table.get("vendor-modules", ())),
                brand_names=tuple(str(m).lower() for m in table.get("brand-names", ())),
                entry_points=dict(project.get("entry-points", {})),
            )
        )
    return found


def forbidden_modules(found: Iterable[Adapter]) -> set[str]:
    """The adapter packages and the vendor modules they declare (never generic libraries such
    as an HTTP client the core may use too)."""
    return {m for a in found for m in (a.module, *a.vendor_modules)}


def vendor_words(found: Iterable[Adapter]) -> list[str]:
    """Lower-case words that name a vendor, longest first."""
    words = {w.lower() for a in found for w in (a.vendor, *a.vendor_modules, *a.brand_names)}
    return sorted(words, key=lambda w: (-len(w), w))


def _vendor_pattern(words: Iterable[str]) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE)


def core_sources(repo_root: Path, found: Iterable[Adapter]) -> Iterator[Path]:
    """Python files under ``src`` of every service, non-adapter library and integration."""
    skip = {a.path for a in found}
    for group in ("services", "libs", "integrations"):
        base = repo_root / group
        if not base.is_dir():
            continue
        for package in sorted(base.iterdir()):
            src = package / "src"
            if package in skip or not src.is_dir():
                continue
            yield from (p for p in sorted(src.rglob("*.py")) if "__pycache__" not in p.parts)


def imported_modules(tree: ast.AST) -> set[str]:
    """Top-level module names imported anywhere in ``tree`` (absolute imports)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".", 1)[0])
    return names


def dynamic_loads(tree: ast.AST) -> list[int]:
    """Lines of ``importlib.import_module``, ``__import__`` and ``<entry point>.load()`` calls."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in {"import_module", "__import__"}:
            lines.append(node.lineno)
        elif name == "load" and isinstance(func, ast.Attribute):
            target = ast.unparse(func.value).lower()
            if "entry" in target or target in {"ep", "point"}:
                lines.append(node.lineno)
    return lines


def string_literals(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """Every string constant that is not a docstring (a bare string statement)."""
    bare = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in bare:
            yield node.lineno, node.value


_COMMENTS = re.compile(r"<!--.*?-->|/\*.*?\*/|//[^\n]*", re.S)


def web_texts(repo_root: Path) -> Iterator[tuple[str, str]]:
    """``(file, text)`` of the web app's messages (each value) and sources (comments removed;
    unit tests and generated files excluded)."""
    for messages in sorted((repo_root / "apps").glob("*/messages/*.json")):
        for key, value in json.loads(messages.read_text("utf-8")).items():
            if isinstance(value, str):
                yield f"{messages.relative_to(repo_root).as_posix()}#{key}", value
    for app in sorted((repo_root / "apps").iterdir()):
        src = app / "src"
        if not src.is_dir():
            continue
        for source in sorted(src.rglob("*")):
            if source.suffix not in {".ts", ".svelte"} or ".test." in source.name:
                continue
            if {"generated", "paraglide", "node_modules"} & set(source.parts):
                continue
            text = _COMMENTS.sub("", source.read_text("utf-8"))
            yield source.relative_to(repo_root).as_posix(), text


# --- the scanners themselves ----------------------------------------------------------------


def test_vendor_isolation_scanners(tmp_path: Path) -> None:
    source = (
        '"""Docstrings may name Acme."""\n'
        "import os\n"
        "import importlib\n"
        "from acme_sdk.client import Client\n"
        "def f(entry_point):\n"
        "    import mowdeck_control_acme.plugin as plugin\n"
        "    importlib.import_module('x')\n"
        "    entry_point.load()\n"
        "    return 'talks to the Acme cloud'\n"
        "from . import sibling\n"
    )
    tree = ast.parse(source)
    assert imported_modules(tree) == {"os", "importlib", "acme_sdk", "mowdeck_control_acme"}
    assert dynamic_loads(tree) == [7, 8]
    pattern = _vendor_pattern(["acme"])
    assert [text for _, text in string_literals(tree) if pattern.search(text)] == [
        "talks to the Acme cloud"
    ]
    assert _COMMENTS.sub("", "<!-- Acme --> a /* Acme */ b // Acme\nc") == " a  b \nc"


def test_vendor_isolation_reads_the_adapter_declarations(repo_root: Path) -> None:
    found = adapters(repo_root)
    assert found, "no adapter package under libs/"
    modules = forbidden_modules(found)
    assert {"mowdeck_control_mammotion", "pymammotion"} <= modules
    assert not modules & {"mowdeck_control", "mowdeck_contracts", "httpx", "aiohttp", "pydantic"}
    assert {"mammotion", "pymammotion", "luba"} <= set(vendor_words(found))


# --- the core ---------------------------------------------------------------------------------


def test_vendor_isolation_core_imports_no_vendor_package(repo_root: Path) -> None:
    found = adapters(repo_root)
    forbidden = forbidden_modules(found)
    files = list(core_sources(repo_root, found))
    assert len(files) > 100  # the scan really covers the services
    offenders = []
    for path in files:
        hits = imported_modules(ast.parse(path.read_text("utf-8"))) & forbidden
        if hits:
            offenders.append(f"{path.relative_to(repo_root).as_posix()}: {', '.join(sorted(hits))}")
    assert not offenders, (
        "core code imports a vendor package; plug it in through an entry point "
        f"({', '.join(ENTRY_POINT_GROUPS)}) instead:\n" + "\n".join(offenders)
    )


def test_vendor_isolation_core_loads_code_only_in_the_plugin_loaders(repo_root: Path) -> None:
    offenders = []
    for path in core_sources(repo_root, adapters(repo_root)):
        relative = path.relative_to(repo_root).as_posix()
        lines = dynamic_loads(ast.parse(path.read_text("utf-8")))
        if lines and relative not in DYNAMIC_LOADERS:
            offenders.append(f"{relative}: lines {', '.join(map(str, lines))}")
    assert not offenders, "dynamic code loading outside the plugin loaders:\n" + "\n".join(
        offenders
    )
    for relative in DYNAMIC_LOADERS:
        path = repo_root / relative
        assert path.is_file() and dynamic_loads(ast.parse(path.read_text("utf-8"))), (
            f"{relative}: drop it from DYNAMIC_LOADERS"
        )


def test_vendor_isolation_core_strings_name_no_vendor(repo_root: Path) -> None:
    found = adapters(repo_root)
    pattern = _vendor_pattern(vendor_words(found))
    offenders = []
    used: set[tuple[str, str]] = set()
    for path in core_sources(repo_root, found):
        relative = path.relative_to(repo_root).as_posix()
        for line, text in string_literals(ast.parse(path.read_text("utf-8"))):
            if not pattern.search(text):
                continue
            if (relative, text) in ALLOWED_STRINGS:
                used.add((relative, text))
                continue
            offenders.append(f"{relative}:{line}: {text[:80]!r}")
    assert not offenders, (
        "core strings name a vendor; say it neutrally or let the adapter declare it "
        "(descriptor, options, lexicon):\n" + "\n".join(offenders)
    )
    assert used == set(ALLOWED_STRINGS), "drop unused entries from ALLOWED_STRINGS"


def test_vendor_isolation_web_app_names_no_vendor(repo_root: Path) -> None:
    pattern = _vendor_pattern(vendor_words(adapters(repo_root)))
    offenders = [
        f"{where}: {match.group(0)}"
        for where, text in web_texts(repo_root)
        if (match := pattern.search(text))
    ]
    assert not offenders, (
        "the web app names a vendor outside comments and unit tests; use neutral words or "
        "the provider's name from its descriptor:\n" + "\n".join(offenders)
    )


def test_vendor_isolation_only_the_gateway_depends_on_adapters(repo_root: Path) -> None:
    names = {a.path.name for a in adapters(repo_root)}
    for service in sorted((repo_root / "services").iterdir()):
        if not (service / "pyproject.toml").is_file():
            continue
        project = tomllib.loads((service / "pyproject.toml").read_text("utf-8"))["project"]
        dependencies = {
            re.split(r"[\s\[<>=!~;]", str(r), maxsplit=1)[0].strip().lower()
            for r in project.get("dependencies", [])
        }
        if service.name not in ADAPTER_HOSTS:
            assert not dependencies & names, f"{service.name} depends on {dependencies & names}"


# --- the adapters ----------------------------------------------------------------------------


def test_vendor_isolation_adapters_reach_mowers_and_ship_a_lexicon(repo_root: Path) -> None:
    for adapter in adapters(repo_root):
        entry_points = adapter.entry_points
        assert any(entry_points.get(g) for g in LINK_GROUPS), (
            f"{adapter.path.name} registers nothing under {' or '.join(LINK_GROUPS)}"
        )
        assert entry_points.get("mowdeck.error_lexicons"), (
            f"{adapter.path.name} registers nothing under mowdeck.error_lexicons"
        )
        assert adapter.vendor_modules, (
            f"{adapter.path.name}: declare [tool.mowdeck.adapter] vendor-modules"
        )


@pytest.mark.parametrize("group", ENTRY_POINT_GROUPS)
def test_vendor_isolation_entry_points_live_in_their_own_package(
    repo_root: Path, group: str
) -> None:
    for adapter in adapters(repo_root):
        for name, target in adapter.entry_points.get(group, {}).items():
            module = target.split(":", 1)[0].split(".", 1)[0]
            assert module == adapter.module, f"{adapter.path.name}: {group} {name} -> {target}"


def test_vendor_isolation_adapters_do_not_import_each_other(repo_root: Path) -> None:
    found = adapters(repo_root)
    for adapter in found:
        others = {a.module for a in found if a is not adapter}
        for path in sorted((adapter.path / "src").rglob("*.py")):
            hits = imported_modules(ast.parse(path.read_text("utf-8"))) & others
            assert not hits, f"{path.relative_to(repo_root).as_posix()} imports {hits}"


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
