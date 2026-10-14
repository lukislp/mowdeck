"""Every service and library must be a documented, self-describing package."""

import json
from pathlib import Path

import pytest


def _package_dirs(repo_root: Path) -> list[Path]:
    dirs: list[Path] = []
    for group in ("services", "libs"):
        base = repo_root / group
        if base.is_dir():
            dirs.extend(
                p for p in sorted(base.iterdir()) if p.is_dir() and not p.name.startswith(".")
            )
    return dirs


def test_packages_have_readme_and_pyproject(repo_root: Path) -> None:
    for package in _package_dirs(repo_root):
        assert (package / "README.md").is_file(), f"{package.name}: README.md missing"
        # pnpm packages (libs/mowdeck-ui) have a package.json instead of a pyproject.toml.
        manifest = "package.json" if (package / "package.json").is_file() else "pyproject.toml"
        assert (package / manifest).is_file(), f"{package.name}: {manifest} missing"


def test_libs_have_apache_license(repo_root: Path) -> None:
    for package in _package_dirs(repo_root):
        if package.parent.name == "libs":
            assert (package / "LICENSE").is_file(), f"{package.name}: LICENSE (Apache-2.0) missing"


def test_group_directories_exist(repo_root: Path) -> None:
    for group in ("services", "libs"):
        assert (repo_root / group).is_dir()


PNPM_PACKAGES = ["apps/web", "libs/mowdeck-ui", "tests/e2e-web", "tools/ts-codegen"]


@pytest.mark.parametrize("path", PNPM_PACKAGES)
def test_pnpm_packages_pin_exact_versions(repo_root: Path, path: str) -> None:
    manifest = json.loads((repo_root / path / "package.json").read_text(encoding="utf-8"))
    for group in ("dependencies", "devDependencies"):
        for name, version in manifest.get(group, {}).items():
            exact = version.startswith("workspace:") or version[:1].isdigit()
            assert exact, f"{path}: {name} {version!r} is not pinned to an exact version"


def test_pnpm_workspace_lists_the_frontend_packages(repo_root: Path) -> None:
    workspace = (repo_root / "pnpm-workspace.yaml").read_text(encoding="utf-8")
    for entry in ("apps/*", "libs/mowdeck-ui", "tests/e2e-web", "tools/ts-codegen"):
        assert f"- {entry}" in workspace, f"pnpm-workspace.yaml misses {entry}"
    assert (repo_root / "pnpm-lock.yaml").is_file(), "pnpm-lock.yaml must be committed"
    root = json.loads((repo_root / "package.json").read_text(encoding="utf-8"))
    assert root["packageManager"].startswith("pnpm@")
