"""The provider template renders a package that passes the conformance kit out of the box.

``templates/provider`` (ADR 0069) is rendered into a temporary directory and the generated
package is made importable there without installing it: its source directory goes on the path
and a ``.dist-info`` directory carries the entry points of its ``pyproject.toml``, which is all
``importlib.metadata`` needs. Its own tests (the provider conformance kit and the connection
tests) then run in a fresh interpreter, and ruff and mypy (strict) check the generated code, so
the template cannot rot.
"""

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

copier = pytest.importorskip("copier")

VENDOR = "acme"
DISTRIBUTION = f"mowdeck-control-{VENDOR}"
PACKAGE = f"mowdeck_control_{VENDOR}"
FILES = (
    ".copier-answers.yml",
    "LICENSE",
    "README.md",
    "pyproject.toml",
    f"src/{PACKAGE}/__init__.py",
    f"src/{PACKAGE}/py.typed",
    f"src/{PACKAGE}/client.py",
    f"src/{PACKAGE}/connection.py",
    f"src/{PACKAGE}/descriptor.py",
    f"src/{PACKAGE}/fake_client.py",
    f"src/{PACKAGE}/lexicon.py",
    f"src/{PACKAGE}/plugin.py",
    f"src/{PACKAGE}/provider.py",
    f"src/{PACKAGE}/sdk_client.py",
    f"tests/test_{VENDOR}_conformance.py",
    f"tests/test_{VENDOR}_connection.py",
)


def _render(repo_root: Path, destination: Path, **answers: Any) -> Path:
    copier.run_copy(
        str(repo_root / "templates" / "provider"),
        str(destination),
        data=answers,
        defaults=True,
        unsafe=True,
        quiet=True,
    )
    return destination


def _project(package: Path) -> dict[str, Any]:
    data: dict[str, Any] = tomllib.loads((package / "pyproject.toml").read_text("utf-8"))
    return data


def _install_metadata(package: Path, site: Path) -> None:
    """A ``.dist-info`` with the name, version and entry points of the rendered package."""
    project = _project(package)["project"]
    info = site / f"{project['name'].replace('-', '_')}-{project['version']}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {project['name']}\nVersion: {project['version']}\n",
        encoding="utf-8",
    )
    lines: list[str] = []
    for group, entries in project["entry-points"].items():
        lines.append(f"[{group}]")
        lines.extend(f"{name} = {target}" for name, target in entries.items())
        lines.append("")
    (info / "entry_points.txt").write_text("\n".join(lines), encoding="utf-8")


def _run(command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - this interpreter and its tools, generated code
        command, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=600
    )


@pytest.fixture(scope="module")
def rendered(repo_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("provider")
    return _render(
        repo_root,
        base / DISTRIBUTION,
        vendor=VENDOR,
        vendor_name="Acme Robotics",
        sdk_module="acme_cloud",
        sdk_licence="permissive",
        brand_names=["roadrunner"],
    )


def test_template_renders_a_complete_apache_package(repo_root: Path, rendered: Path) -> None:
    for relative in FILES:
        assert (rendered / relative).is_file(), f"missing {relative}"
    data = _project(rendered)
    project = data["project"]
    assert project["name"] == DISTRIBUTION
    assert project["license"] == "Apache-2.0"
    assert project["requires-python"] == ">=3.13,<3.14"
    assert project["entry-points"]["mowdeck.cloud_providers"] == {VENDOR: f"{PACKAGE}.plugin:SPEC"}
    assert project["entry-points"]["mowdeck.error_lexicons"] == {
        VENDOR: f"{PACKAGE}.lexicon:{VENDOR}_lexicon"
    }
    adapter = data["tool"]["mowdeck"]["adapter"]
    assert adapter == {"vendor-modules": ["acme_cloud"], "brand-names": ["roadrunner"]}
    licence = (rendered / "LICENSE").read_text("utf-8")
    assert licence == (repo_root / "LICENSES" / "Apache-2.0.txt").read_text("utf-8")
    descriptor = (rendered / "src" / PACKAGE / "descriptor.py").read_text("utf-8")
    assert 'name="Acme Robotics"' in descriptor


def test_template_makes_a_gpl_sdk_a_gpl_package(repo_root: Path, tmp_path: Path) -> None:
    package = _render(repo_root, tmp_path / "gpl", vendor="gnumow", sdk_licence="gpl")
    assert _project(package)["project"]["license"] == "GPL-3.0-only"
    licence = (package / "LICENSE").read_text("utf-8")
    assert licence == (repo_root / "LICENSES" / "GPL-3.0.txt").read_text("utf-8")
    assert "must be GPL-3.0 itself" in (package / "README.md").read_text("utf-8")


def test_template_asks_for_the_sdk_licence(repo_root: Path, tmp_path: Path) -> None:
    """No default: the licence of the SDK, and so of the package, is a conscious choice."""
    with pytest.raises(ValueError, match="sdk_licence"):
        _render(repo_root, tmp_path / "unknown", vendor="acme")


def test_template_rejects_invalid_vendor_keys(repo_root: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lower-case letters and digits"):
        _render(repo_root, tmp_path / "bad", vendor="Acme-Mowers", sdk_licence="permissive")


@pytest.mark.timeout(600)
def test_rendered_package_passes_its_tests_and_the_linters(
    repo_root: Path, rendered: Path, tmp_path: Path
) -> None:
    site = tmp_path / "site"
    _install_metadata(rendered, site)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(site), str(rendered / "src")]),
        "MYPYPATH": str(rendered / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    tests = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "asyncio_mode=auto",
            "--rootdir",
            str(rendered),
        ],
        rendered,
        env,
    )
    assert tests.returncode == 0, tests.stdout[-6000:] + tests.stderr[-3000:]
    assert " passed" in tests.stdout and " failed" not in tests.stdout, tests.stdout[-3000:]

    config = str(repo_root / "pyproject.toml")
    lint = _run([sys.executable, "-m", "ruff", "check", "--config", config, "."], rendered, env)
    assert lint.returncode == 0, lint.stdout + lint.stderr
    fmt = _run(
        [sys.executable, "-m", "ruff", "format", "--check", "--config", config, "."], rendered, env
    )
    assert fmt.returncode == 0, fmt.stdout + fmt.stderr
    types = _run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--python-version",
            "3.13",
            "--cache-dir",
            str(tmp_path / "mypy-cache"),
            f"src/{PACKAGE}",
            "tests",
        ],
        rendered,
        env,
    )
    assert types.returncode == 0, types.stdout + types.stderr
