"""The release attaches only its own files (scripts/release_assets.py, job publish)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

VERSION = "1.2.3"
DOWNLOAD_PATTERN = "{digest-*,sbom-*.spdx.json,chart,android-apk}"


@pytest.fixture(scope="module")
def assets(repo_root: Path) -> ModuleType:
    """The script as a module."""
    path = repo_root / "tools" / "repo-checks" / "scripts" / "release_assets.py"
    spec = importlib.util.spec_from_file_location("release_assets", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def workflow(repo_root: Path) -> dict[str, Any]:
    """The parsed release workflow."""
    text = (repo_root / ".github" / "workflows" / "release.yml").read_text("utf-8")
    data: dict[str, Any] = yaml.safe_load(text)
    return data


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


def test_no_build_record_artifacts(workflow: dict[str, Any]) -> None:
    builds = [
        s for s in _steps(workflow) if s.get("uses", "").startswith("docker/build-push-action@")
    ]
    assert builds
    for step in builds:
        env = step.get("env") or {}
        assert env.get("DOCKER_BUILD_RECORD_UPLOAD") == "false", step.get("name")


def test_publish_downloads_only_the_release_artifacts(workflow: dict[str, Any]) -> None:
    steps = workflow["jobs"]["publish"]["steps"]
    downloads = [s for s in steps if s.get("uses", "").startswith("actions/download-artifact@")]
    assert len(downloads) == 1
    options = downloads[0]["with"]
    assert options["pattern"] == DOWNLOAD_PATTERN
    assert "name" not in options
    assert options["path"] != "assets"


def test_publish_selects_and_checks_before_the_upload(workflow: dict[str, Any]) -> None:
    runs = [s.get("run", "") for s in workflow["jobs"]["publish"]["steps"]]
    select = next(i for i, r in enumerate(runs) if "release_assets.py select" in r)
    sums = next(i for i, r in enumerate(runs) if "sha256sum" in r)
    check = next(i for i, r in enumerate(runs) if "release_assets.py check" in r)
    upload = next(i for i, r in enumerate(runs) if "gh release upload" in r)
    assert select <= sums < check <= upload
    # The checksums follow the selection in the same step.
    assert runs[sums].index("release_assets.py select") < runs[sums].index("sha256sum")
    # Signatures and provenance are written before the check runs.
    assert runs[check].index("cosign sign-blob") < runs[check].index("release_assets.py check")
    assert 'gh release upload "$tag" -R "$REPOSITORY" --clobber assets/*' in runs[upload]


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", "utf-8")


def test_select_keeps_only_release_files(assets: ModuleType, tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    for name in [
        "digest-identity.txt",
        "digest-chart.txt",
        "sbom-identity.spdx.json",
        "sbom-device-gateway.spdx.json",
        f"mowdeck-{VERSION}.tgz",
        f"mowdeck-{VERSION}.apk",
        "lukislp~mowdeck~1D3ZWP.dockerbuild",
        "lukislp.mowdeck.1D3ZWP.dockerbuild",
        "mowdeck-0.0.1.tgz",
        "nested/sbom-identity.spdx.json",
    ]:
        _touch(downloads / name)

    ignored = assets.select(VERSION, downloads, tmp_path / "assets", tmp_path / "digests")

    assert sorted(p.name for p in (tmp_path / "assets").iterdir()) == [
        f"mowdeck-{VERSION}.apk",
        f"mowdeck-{VERSION}.tgz",
        "sbom-device-gateway.spdx.json",
        "sbom-identity.spdx.json",
    ]
    assert sorted(p.name for p in (tmp_path / "digests").iterdir()) == [
        "digest-chart.txt",
        "digest-identity.txt",
    ]
    assert ignored == [
        "lukislp.mowdeck.1D3ZWP.dockerbuild",
        "lukislp~mowdeck~1D3ZWP.dockerbuild",
        "mowdeck-0.0.1.tgz",
        "nested/sbom-identity.spdx.json",
    ]


def test_select_needs_the_chart_and_an_sbom(assets: ModuleType, tmp_path: Path) -> None:
    _touch(tmp_path / "downloads" / "sbom-identity.spdx.json")
    with pytest.raises(SystemExit, match="chart"):
        assets.select(VERSION, tmp_path / "downloads", tmp_path / "a", tmp_path / "d")
    _touch(tmp_path / "only-chart" / f"mowdeck-{VERSION}.tgz")
    with pytest.raises(SystemExit, match="sbom"):
        assets.select(VERSION, tmp_path / "only-chart", tmp_path / "b", tmp_path / "e")


def test_check_allows_the_signed_release_files_only(assets: ModuleType, tmp_path: Path) -> None:
    good = [
        f"mowdeck-{VERSION}.tgz",
        f"mowdeck-{VERSION}.apk",
        "sbom-identity.spdx.json",
        "SOURCE-OFFER.md",
        "SHA256SUMS",
        f"mowdeck-{VERSION}.intoto.jsonl",
    ]
    for name in good:
        _touch(tmp_path / name)
        if not name.endswith(".intoto.jsonl"):
            _touch(tmp_path / f"{name}.sigstore.json")
    assert assets.check(VERSION, tmp_path) == []

    for name in ["lukislp.mowdeck.1D3ZWP.dockerbuild", "x.dockerbuild.sigstore.json", "notes.md"]:
        _touch(tmp_path / name)
    assert assets.check(VERSION, tmp_path) == [
        "lukislp.mowdeck.1D3ZWP.dockerbuild",
        "notes.md",
        "x.dockerbuild.sigstore.json",
    ]
    assert assets.main(["check", "--version", VERSION, "--assets", str(tmp_path)]) == 1
