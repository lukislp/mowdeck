# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Reproducible builds (docs/dev/reproducible-builds.md).

* ``scripts/compare_builds.py`` compares two build outputs file by file, without exceptions;
* ``.github/workflows/reproducibility.yml`` checks every image the release publishes and runs
  on the inputs of the build;
* the Dockerfiles and the release workflow pass SOURCE_DATE_EPOCH (the commit time), which the
  repeatable output depends on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(".github") / "workflows"


@pytest.fixture(scope="module")
def compare_builds(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "repo-checks" / "scripts" / "compare_builds.py"
    spec = importlib.util.spec_from_file_location("compare_builds", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def _workflow(repo_root: Path, name: str) -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load((repo_root / WORKFLOWS / name).read_text("utf-8"))
    return data


def _on(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML reads the key "on" as the boolean True.
    on = workflow["on"] if "on" in workflow else workflow[True]
    assert isinstance(on, dict)
    return on


def _matrix(workflow: dict[Any, Any], job: str) -> list[dict[str, str]]:
    include: list[dict[str, str]] = workflow["jobs"][job]["strategy"]["matrix"]["include"]
    return [{k: entry[k] for k in ("image", "context", "file", "kind")} for entry in include]


# --- compare_builds.py --------------------------------------------------------------------------


def test_identical_builds_pass(
    compare_builds: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {"web/index.html": b"<html>", "python/a-1.0-py3-none-any.whl": b"PK", "x": b""}
    first = _tree(tmp_path / "a", files)
    second = _tree(tmp_path / "b", files)
    summary = tmp_path / "summary.md"
    assert compare_builds.main([str(first), str(second), "--summary", str(summary)]) == 0
    assert "3/3 files bit-identical" in capsys.readouterr().out
    assert "3/3 files bit-identical" in summary.read_text("utf-8")


def test_a_changed_file_fails(compare_builds: ModuleType, tmp_path: Path) -> None:
    first = _tree(tmp_path / "a", {"web/_app/version.json": b"1", "same": b"s"})
    second = _tree(tmp_path / "b", {"web/_app/version.json": b"2", "same": b"s"})
    assert compare_builds.main([str(first), str(second)]) == 1
    differences = compare_builds.compare(
        compare_builds.manifest(first), compare_builds.manifest(second)
    )
    assert differences == [("web/_app/version.json", "content differs")]


def test_a_missing_file_fails(compare_builds: ModuleType, tmp_path: Path) -> None:
    first = _tree(tmp_path / "a", {"one": b"1", "two": b"2"})
    second = _tree(tmp_path / "b", {"one": b"1", "three": b"3"})
    assert compare_builds.main([str(first), str(second)]) == 1
    differences = compare_builds.compare(
        compare_builds.manifest(first), compare_builds.manifest(second)
    )
    assert differences == [
        ("three", "only in the second build"),
        ("two", "only in the first build"),
    ]


def test_empty_or_missing_builds_are_errors(compare_builds: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert compare_builds.main([str(tmp_path / "a"), str(tmp_path / "b")]) == 2
    assert compare_builds.main([str(tmp_path / "a"), str(tmp_path / "missing")]) == 2


# --- the workflow -------------------------------------------------------------------------------


def test_every_released_image_is_checked(repo_root: Path) -> None:
    release = _matrix(_workflow(repo_root, "release.yml"), "images")
    checked = _matrix(_workflow(repo_root, "reproducibility.yml"), "images")
    assert checked == release, "the images matrix must equal the one of release.yml"


def test_the_check_runs_weekly_and_on_build_inputs(repo_root: Path) -> None:
    on = _on(_workflow(repo_root, "reproducibility.yml"))
    assert on["schedule"]
    assert "workflow_dispatch" in on
    paths = set(on["pull_request"]["paths"])
    for path in (
        ".github/workflows/reproducibility.yml",
        "tools/repo-checks/scripts/compare_builds.py",
        "tools/repo-checks/scripts/reproducible_build.sh",
        "**/Dockerfile",
        "uv.lock",
        "pnpm-lock.yaml",
        "apps/web/vite.config.ts",
        "infra/helm/mowdeck/Chart.yaml",
    ):
        assert path in paths, path


def test_the_check_has_no_exceptions(repo_root: Path) -> None:
    text = (repo_root / WORKFLOWS / "reproducibility.yml").read_text("utf-8")
    assert "compare_builds.py" in text
    assert "--known" not in text
    assert "rewrite-timestamp=true" in text
    assert "--no-cache" in text


# --- SOURCE_DATE_EPOCH in the Dockerfiles and the release --------------------------------------


def test_python_images_build_repeatable_venvs(repo_root: Path) -> None:
    release = _matrix(_workflow(repo_root, "release.yml"), "images")
    python = [entry["file"] for entry in release if entry["kind"] == "python"]
    assert python
    template = repo_root / "templates/service/template/{{ image_build_file }}.jinja"
    for file in [*python, template.relative_to(repo_root).as_posix()]:
        text = (repo_root / file).read_text("utf-8")
        assert "ARG SOURCE_DATE_EPOCH" in text, file
        # uv's parallel bytecode workers made .pyc files differ; one process compiles instead.
        assert "UV_COMPILE_BYTECODE" not in text, file
        assert "compileall.compile_dir('/app/.venv/lib', force=True, quiet=2, workers=1)" in text, (
            file
        )
        assert "PYTHONHASHSEED=0" in text, file
        assert "uv_cache.json" in text, file


def test_the_web_build_reads_source_date_epoch(repo_root: Path) -> None:
    dockerfile = (repo_root / "apps/web/Dockerfile").read_text("utf-8")
    build_stage = dockerfile.split("FROM ${NGINX_IMAGE}")[0]
    assert "ARG SOURCE_DATE_EPOCH" in build_stage
    assert build_stage.index("ARG SOURCE_DATE_EPOCH") < build_stage.index("pnpm --filter web build")
    config = (repo_root / "apps/web/vite.config.ts").read_text("utf-8")
    assert "process.env.SOURCE_DATE_EPOCH" in config
    assert "version: { name: versionName" in config


def test_the_release_builds_with_source_date_epoch(repo_root: Path) -> None:
    release = _workflow(repo_root, "release.yml")["jobs"]
    images = release["images"]["steps"]
    args = next(step for step in images if step.get("id") == "args")
    assert "SOURCE_DATE_EPOCH=" in args["run"]
    build = next(step for step in images if step.get("id") == "build")
    assert build["with"]["outputs"] == "type=image,rewrite-timestamp=true"
    assert build["with"]["push"] is True
    package = next(
        step for step in release["chart"]["steps"] if "helm package" in step.get("run", "")
    )
    assert "SOURCE_DATE_EPOCH" in package["run"]
