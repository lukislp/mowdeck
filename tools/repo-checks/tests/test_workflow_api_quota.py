"""The workflows stay within GITHUB_TOKEN's API quota (1,000 REST calls per hour per repository).

* no docker/build-push-action step uploads a build record artifact (``*.dockerbuild``): every
  artifact makes ClusterFuzzLite's corpus lookup, which pages through all artifacts, dearer;
* pull request fuzzing runs only when fuzzed code changes, the scheduled runs stay;
* the OpenSSF Scorecard runs on a schedule, not after every ci run, one at a time;
* the in-repo ``gh`` calls of the release workflows retry on the rate limit
  (``scripts/gh_retry.sh``), which is tested here against a fake ``gh``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(".github") / "workflows"
FUZZ_PR_PATHS = {
    "fuzz/**",
    ".clusterfuzzlite/**",
    "libs/**",
    "services/**",
    "integrations/**",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/fuzzing.yml",
}


def _load(repo_root: Path, name: str) -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load((repo_root / WORKFLOWS / name).read_text("utf-8"))
    return data


def _on(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML reads the key "on" as the boolean True.
    on = workflow["on"] if "on" in workflow else workflow[True]
    assert isinstance(on, dict)
    return on


def _steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


def test_no_workflow_uploads_docker_build_records(repo_root: Path) -> None:
    builders = []
    for path in sorted((repo_root / WORKFLOWS).glob("*.yml")):
        workflow = _load(repo_root, path.name)
        uses = [s.get("uses", "") for s in _steps(workflow)]
        if any(u.startswith("docker/build-push-action@") for u in uses):
            builders.append(path.name)
            env = workflow.get("env") or {}
            assert env.get("DOCKER_BUILD_RECORD_UPLOAD") == "false", path.name
            assert "DOCKER_BUILD_SUMMARY" not in env, path.name
    assert {"ci.yml", "helm-kind.yml", "release.yml"} <= set(builders)


def test_short_lived_ci_artifacts_have_a_retention(repo_root: Path) -> None:
    workflow = _load(repo_root, "ci.yml")
    uploads = [
        s for s in _steps(workflow) if s.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert uploads
    for step in uploads:
        days = step["with"].get("retention-days")
        assert isinstance(days, int), step["with"]["name"]
        assert 1 <= days <= 7, step["with"]["name"]


def test_pull_request_fuzzing_only_for_fuzzed_code(repo_root: Path) -> None:
    on = _on(_load(repo_root, "fuzzing.yml"))
    assert set(on["pull_request"]["paths"]) == FUZZ_PR_PATHS
    crons = {entry["cron"] for entry in on["schedule"]}
    assert crons == {"23 3 * * *", "47 4 * * 0"}
    assert "workflow_dispatch" in on


def test_fuzzed_sources_are_covered_by_the_paths(repo_root: Path) -> None:
    """Every source directory the ClusterFuzzLite build bundles triggers pull request fuzzing."""
    build = (repo_root / ".clusterfuzzlite" / "build.sh").read_text("utf-8")
    srcs = re.search(r"^srcs=\((.*)\)$", build, re.MULTILINE)
    assert srcs is not None
    for src in srcs.group(1).split():
        top = src.split("/")[0]
        assert f"{top}/**" in FUZZ_PR_PATHS, src


def test_scorecard_runs_on_a_schedule_one_at_a_time(repo_root: Path) -> None:
    workflow = _load(repo_root, "scorecard.yml")
    on = _on(workflow)
    assert "workflow_run" not in on
    assert {"branch_protection_rule", "schedule", "workflow_dispatch"} <= set(on)
    crons = [entry["cron"] for entry in on["schedule"]]
    assert any(cron.endswith("* * *") for cron in crons), crons
    assert workflow["concurrency"] == {"group": "scorecard", "cancel-in-progress": True}
    text = (repo_root / WORKFLOWS / "scorecard.yml").read_text("utf-8")
    assert "github.event.workflow_run" not in text
    steps = workflow["jobs"]["analysis"]["steps"]
    scorecard = [s for s in steps if s.get("uses", "").startswith("ossf/scorecard-action@")]
    assert scorecard[0]["with"]["publish_results"] is True


GH_CALL = re.compile(r"(?<![\w-])gh (api|release)\b")


@pytest.mark.parametrize("name", ["release.yml", "apple-testflight.yml"])
def test_api_calls_retry_on_the_rate_limit(repo_root: Path, name: str) -> None:
    calls = 0
    for step in _steps(_load(repo_root, name)):
        script = step.get("run", "")
        lines = [line for line in script.splitlines() if not line.strip().startswith("echo")]
        if not any(GH_CALL.search(line) for line in lines):
            continue
        assert "scripts/gh_retry.sh" in script, step.get("name")
        for line in lines:
            for match in GH_CALL.finditer(line):
                calls += 1
                assert line[: match.start()].endswith("gh_retry "), line.strip()
    assert calls


# --- gh_retry.sh against a fake gh -------------------------------------------------------------


def _find_bash() -> str | None:
    """bash from PATH; on Windows Git Bash (the bash.exe of WSL cannot run these scripts)."""
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if os.name == "nt" and git_bash.exists():
        return str(git_bash)
    return shutil.which("bash")


BASH = _find_bash()

# Answers from FAKE_ANSWERS (one "exit-code|stdout|stderr" per line), one per call; counts calls.
FAKE_GH = """#!/usr/bin/env bash
n=$(( $(cat "$FAKE_COUNT" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAKE_COUNT"
line="$(sed -n "${n}p" "$FAKE_ANSWERS")"
IFS='|' read -r code out err <<<"$line"
[ -n "$out" ] && echo "$out"
[ -n "$err" ] && echo "$err" >&2
exit "$code"
"""


def _run(
    tmp_path: Path, repo_root: Path, answers: list[str]
) -> tuple[subprocess.CompletedProcess[str], int]:
    assert BASH is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gh"
    fake.write_text(FAKE_GH, "utf-8", newline="\n")
    fake.chmod(0o755)
    (tmp_path / "answers").write_text("\n".join(answers) + "\n", "utf-8", newline="\n")
    helper = (repo_root / "tools" / "repo-checks" / "scripts" / "gh_retry.sh").as_posix()
    script = f'set -euo pipefail; . "{helper}"; out="$(gh_retry gh api x)"; printf "<%s>" "$out"'
    env = {
        **os.environ,
        "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_ANSWERS": (tmp_path / "answers").as_posix(),
        "FAKE_COUNT": (tmp_path / "count").as_posix(),
        "GH_RETRY_DELAYS": "0 0 0",
    }
    if os.name == "nt":
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    result = subprocess.run(  # noqa: S603
        [BASH, "-c", script], capture_output=True, text=True, env=env, check=False, timeout=60
    )
    count = int((tmp_path / "count").read_text().strip() or 0)
    return result, count


bash_only = pytest.mark.skipif(BASH is None, reason="bash not installed")


@bash_only
def test_retry_succeeds_after_rate_limits(tmp_path: Path, repo_root: Path) -> None:
    result, calls = _run(
        tmp_path,
        repo_root,
        [
            '1|{"message":"API rate limit exceeded"}|gh: API rate limit exceeded (HTTP 403)',
            "1||You have exceeded a secondary rate limit (HTTP 429)",
            "0|ok|",
        ],
    )
    assert result.returncode == 0, result.stderr
    assert calls == 3
    # Only the answer of the successful attempt reaches the caller.
    assert result.stdout == "<ok>"
    assert result.stderr.count("::warning::") == 2


@bash_only
def test_no_retry_for_other_errors(tmp_path: Path, repo_root: Path) -> None:
    result, calls = _run(
        tmp_path, repo_root, ['1|{"message":"Not Found"}|gh: Not Found (HTTP 404)']
    )
    assert result.returncode != 0
    assert calls == 1
    assert "::error::" in result.stderr
    assert "<" not in result.stdout


@bash_only
def test_gives_up_after_four_attempts(tmp_path: Path, repo_root: Path) -> None:
    limited = "1||gh: API rate limit exceeded (HTTP 403)"
    result, calls = _run(tmp_path, repo_root, [limited] * 6)
    assert result.returncode != 0
    assert calls == 4
    assert "::error::" in result.stderr
