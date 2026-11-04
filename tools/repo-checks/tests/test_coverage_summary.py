# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The coverage table of the CI job summary (tools/repo-checks/scripts/coverage_summary.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
import yaml


def _load(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "repo-checks" / "scripts" / "coverage_summary.py"
    spec = importlib.util.spec_from_file_location("coverage_summary", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_python_report(repo_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _load(repo_root)
    report = {
        "totals": {
            "num_statements": 200,
            "covered_lines": 190,
            "num_branches": 50,
            "covered_branches": 40,
        }
    }
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(report), "utf-8")
    assert script.main(["--python", str(path)]) == 0
    out = capsys.readouterr().out
    assert "| Statements | 95.0 % | 190/200 |" in out
    assert "| Branches | 80.0 % | 40/50 |" in out
    assert "| Combined (coverage.py total) | 92.0 % | 230/250 |" in out


def test_vitest_report(repo_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _load(repo_root)
    entry = {"total": 10, "covered": 9, "skipped": 0, "pct": 90}
    summary = {"total": dict.fromkeys(("lines", "statements", "functions", "branches"), entry)}
    path = tmp_path / "coverage-summary.json"
    path.write_text(json.dumps(summary), "utf-8")
    assert script.main(["--vitest", "apps/web", str(path)]) == 0
    out = capsys.readouterr().out
    assert "### apps/web coverage (Vitest, v8)" in out
    assert "| Statements | 90.0 % | 9/10 |" in out
    assert "| Branches | 90.0 % | 9/10 |" in out


def test_no_branches_and_no_input(repo_root: Path) -> None:
    script = _load(repo_root)
    rows = script.python_rows({"totals": {"num_statements": 0, "covered_lines": 0}})
    assert rows[0] == ("Statements", "n/a", "0/0")
    with pytest.raises(SystemExit):
        script.main([])


def test_tests_are_omitted_from_python_coverage(repo_root: Path) -> None:
    config = tomllib.loads((repo_root / "pyproject.toml").read_text("utf-8"))
    run = config["tool"]["coverage"]["run"]
    assert run["branch"] is True
    assert {"*/tests/*", "tests/*", "*/conftest.py"} <= set(run["omit"])


def test_ci_writes_both_summaries(repo_root: Path) -> None:
    workflow = yaml.safe_load((repo_root / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    runs = {
        job: "\n".join(str(step.get("run", "")) for step in spec["steps"])
        for job, spec in workflow["jobs"].items()
    }
    assert "coverage_summary.py --python" in runs["python"]
    assert "coverage_summary.py --vitest apps/web" in runs["web"]
    vite = (repo_root / "apps" / "web" / "vite.config.ts").read_text("utf-8")
    assert "'json-summary'" in vite
