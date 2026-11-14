# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Pull requests run the ci.yml jobs their changes need, and CI-only changes release nothing.

* ``scripts/ci_scope.py`` maps changed paths to ci.yml jobs: CI configuration needs none of the
  scoped jobs, shared inputs (lock files, libs/, Compose, Dockerfiles) need every dependent job,
  and an unknown path needs every job (fail safe);
* a changed job definition in ci.yml runs that job, a change outside ``jobs`` runs everything;
* ci.yml has no ``paths`` filter (required checks must always report), every scoped job waits
  for ``changes`` and runs unless its output is ``"false"``, and pushes are not scoped;
* Dependabot's GitHub Actions and ClusterFuzzLite bumps are ``ci`` commits, runtime bumps stay
  ``fix(deps)``; ``fix(ci)`` releases nothing (``.github/lktec-bot.yml``) and the release workflow
  skips a release whose changes are CI configuration only.

Policy: docs/dev/ci.md, docs/ops/release.md ("CI-only changes").
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(".github") / "workflows"


@pytest.fixture(scope="module")
def ci_scope(repo_root: Path) -> ModuleType:
    """The classifier script as a module."""
    path = repo_root / "tools" / "repo-checks" / "scripts" / "ci_scope.py"
    spec = importlib.util.spec_from_file_location("ci_scope", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _workflow(repo_root: Path, name: str) -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load((repo_root / WORKFLOWS / name).read_text("utf-8"))
    return data


def _on(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML reads the key "on" as the boolean True.
    on = workflow["on"] if "on" in workflow else workflow[True]
    assert isinstance(on, dict)
    return on


# --- path -> jobs ----------------------------------------------------------------------------

STACK = {"docker", "camera_relay_media", "e2e", "e2e_web", "e2e_demo"}


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/fuzzing.yml",
        ".github/workflows/release.yml",
        ".github/dependabot.yml",
        ".github/CODEOWNERS",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "socket.yml",
        "osv-scanner.toml",
        ".clusterfuzzlite/requirements.txt",
        "tools/repo-checks/scripts/gh_retry.sh",
        "tools/repo-checks/scripts/licence_gate.py",
        "tools/repo-checks/scripts/ci_scope.py",
        "tools/repo-checks/scripts/compare_builds.py",
        "tools/repo-checks/scripts/reproducible_build.sh",
        ".github/workflows/reproducibility.yml",
        "tools/repo-checks/tests/test_dependabot.py",
    ],
)
def test_ci_configuration_needs_no_scoped_job(ci_scope: ModuleType, path: str) -> None:
    assert ci_scope.jobs_for_path(path) == frozenset()
    assert ci_scope.is_ci_path(path)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/ops/release.md", {"docs"}),
        ("mkdocs.yml", {"docs"}),
        ("README.md", {"docs"}),
        ("GOVERNANCE.md", {"docs"}),
        ("ROADMAP.md", {"docs"}),
        ("infra/helm/mowdeck/values.yaml", {"helm"}),
        ("tests/load/report.mjs", {"compose"}),
        ("apps/mobile/src/main.ts", {"web"}),
        ("apps/mobile/ios/MowDeckWatch/MowerView.swift", {"web", "ios"}),
        ("apps/mobile/ios/App/App.xcodeproj/project.pbxproj", {"web", "ios"}),
        ("apps/mobile/scripts/common.sh", {"web", "ios"}),
        ("apps/mobile/package.json", {"web", "ios"}),
        ("apps/mobile/android/gradle/wrapper/gradle-wrapper.jar", {"web", "gradle_wrapper"}),
        ("tests/e2e/test_phase1.py", {"python", "e2e"}),
        ("tests/e2e-web/demo.env", {"web", "compose", "e2e_web", "e2e_demo"}),
        ("apps/web/src/routes/+page.svelte", {"web"} | STACK),
        ("libs/mowdeck-ui/src/Button.svelte", {"web"} | STACK),
        ("services/matter-server/src/index.ts", {"web"} | STACK),
        ("tools/repo-checks/scripts/api_reference.py", {"docs"}),
        ("tools/repo-checks/scripts/export_openapi.py", {"python", "docs", "web"}),
        ("tools/repo-checks/tests/test_helm.py", {"helm"}),
    ],
)
def test_scoped_paths(ci_scope: ModuleType, path: str, expected: set[str]) -> None:
    assert ci_scope.jobs_for_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "services/fleet/src/fleet/app.py",
        "services/identity/Dockerfile",
        "libs/mowdeck-contracts/src/mowdeck_contracts/events.py",
        "libs/mowdeck-control-mammotion/pyproject.toml",
    ],
)
def test_python_sources_and_dockerfiles_run_python_and_the_stack(
    ci_scope: ModuleType, path: str
) -> None:
    jobs = ci_scope.jobs_for_path(path)
    assert {"python", "docs"} | STACK <= jobs
    assert "web" not in jobs


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "pyproject.toml",
        "infra/compose/docker-compose.yml",
        "infra/compose/.env.example",
        "infra/mirror-env.sh",
        "infra/openfga/model.fga",
        "tools/repo-checks/scripts/nats_auth.py",
    ],
)
def test_shared_inputs_run_every_dependent_job(ci_scope: ModuleType, path: str) -> None:
    assert ci_scope.jobs_for_path(path) == ci_scope.ALL - {"web", "gradle_wrapper", "ios"}


@pytest.mark.parametrize(
    "path", ["pnpm-lock.yaml", "package.json", ".node-version", ".corepack-version"]
)
def test_node_inputs_run_web_and_the_stack(ci_scope: ModuleType, path: str) -> None:
    assert ci_scope.jobs_for_path(path) == {"web"} | STACK


@pytest.mark.parametrize(
    "path",
    [
        ".gitattributes",
        "bridge/anything.py",
        "Makefile",
        "new-top-level-dir/file.txt",
        "tools/repo-checks/scripts/a_new_script.py",
    ],
)
def test_unknown_paths_run_every_job(ci_scope: ModuleType, path: str) -> None:
    assert ci_scope.jobs_for_path(path) == ci_scope.ALL
    assert not ci_scope.is_ci_path(path)


def test_ci_only(ci_scope: ModuleType) -> None:
    assert ci_scope.ci_only([".github/workflows/ci.yml", ".github/dependabot.yml"])
    assert not ci_scope.ci_only([".github/workflows/ci.yml", "services/fleet/src/x.py"])
    assert not ci_scope.ci_only(["docs/index.md"])
    assert not ci_scope.ci_only([])  # nothing to compare: never a reason to skip


def test_scope_collects_the_reasons(ci_scope: ModuleType) -> None:
    reasons = ci_scope.scope(["docs/index.md", ".github/dependabot.yml"])
    assert set(reasons) == set(ci_scope.JOBS)
    assert reasons["docs"] == ["docs/index.md"]
    assert all(not paths for job, paths in reasons.items() if job != "docs")


# --- ci.yml job definitions ------------------------------------------------------------------

BASE = """
name: ci
on: {pull_request: {}}
env: {A: "1"}
jobs:
  changes: {runs-on: x, steps: [{run: classify}]}
  python:
    runs-on: x
    steps:
      - &login {name: login, run: login}
      - run: test
  docker:
    runs-on: x
    steps: [*login, {run: build}]
  e2e-web-shard: {runs-on: x, steps: [{run: shard}]}
"""


def test_a_changed_job_runs_that_job(ci_scope: ModuleType) -> None:
    head = BASE.replace("run: build", "run: build --pull")
    assert ci_scope.changed_workflow_jobs(BASE, head) == {"docker"}
    head = BASE.replace("run: shard", "run: shard --retries 2")
    assert ci_scope.changed_workflow_jobs(BASE, head) == {"e2e_web"}
    assert ci_scope.changed_workflow_jobs(BASE, BASE) == frozenset()


def test_a_changed_anchor_runs_every_job_that_uses_it(ci_scope: ModuleType) -> None:
    head = BASE.replace("run: login", "run: login --retry")
    assert ci_scope.changed_workflow_jobs(BASE, head) == {"python", "docker"}


def test_the_classifier_job_itself_runs_nothing_extra(ci_scope: ModuleType) -> None:
    head = BASE.replace("run: classify", "run: classify --verbose")
    assert ci_scope.changed_workflow_jobs(BASE, head) == frozenset()


@pytest.mark.parametrize(
    "head",
    [
        BASE.replace('A: "1"', 'A: "2"'),  # workflow env
        BASE.replace("pull_request: {}", "pull_request: {}, push: {}"),  # triggers
        BASE + "  brand-new-job: {runs-on: x, steps: [{run: y}]}\n",  # unknown job
        "jobs: [not, a, mapping]",
        "jobs: {python: [unclosed",
    ],
)
def test_changes_outside_known_jobs_run_everything(ci_scope: ModuleType, head: str) -> None:
    assert ci_scope.changed_workflow_jobs(BASE, head) == ci_scope.ALL


def test_a_new_or_deleted_workflow_runs_everything(ci_scope: ModuleType) -> None:
    assert ci_scope.changed_workflow_jobs(None, BASE) == ci_scope.ALL
    assert ci_scope.changed_workflow_jobs(BASE, None) == ci_scope.ALL


def test_scope_adds_the_changed_workflow_jobs(ci_scope: ModuleType) -> None:
    head = BASE.replace("run: build", "run: build --pull")
    reasons = ci_scope.scope([ci_scope.WORKFLOW], BASE, head)
    assert [job for job, why in reasons.items() if why] == ["docker"]


# --- the real ci.yml -------------------------------------------------------------------------


def test_every_ci_job_is_mapped(repo_root: Path, ci_scope: ModuleType) -> None:
    jobs = set(_workflow(repo_root, "ci.yml")["jobs"])
    assert jobs == set(ci_scope.WORKFLOW_JOBS), "update WORKFLOW_JOBS in scripts/ci_scope.py"
    outputs = {o for o in ci_scope.WORKFLOW_JOBS.values() if o is not None}
    assert outputs == set(ci_scope.JOBS)


def test_ci_always_starts(repo_root: Path) -> None:
    """A workflow that does not start leaves its required checks pending forever."""
    on = _on(_workflow(repo_root, "ci.yml"))
    assert set(on) == {"push", "pull_request", "workflow_dispatch"}
    for event in ("push", "pull_request"):
        assert not {"paths", "paths-ignore"} & set(on[event] or {}), event


def test_changes_job_outputs_every_job(repo_root: Path, ci_scope: ModuleType) -> None:
    job = _workflow(repo_root, "ci.yml")["jobs"]["changes"]
    assert set(job["outputs"]) == set(ci_scope.JOBS)
    for name, value in job["outputs"].items():
        assert value == f"${{{{ steps.scope.outputs.{name} }}}}"
    run = job["steps"][-1]["run"]
    assert "ci_scope.py jobs" in run
    assert "--all" in run  # pushes and manual runs are not scoped
    assert "--base HEAD^1 --head HEAD" in run
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 2
    assert checkout["with"]["persist-credentials"] is False


def test_scoped_jobs_wait_for_changes_and_fail_safe(repo_root: Path, ci_scope: ModuleType) -> None:
    jobs = _workflow(repo_root, "ci.yml")["jobs"]
    for name, output in ci_scope.WORKFLOW_JOBS.items():
        if output is None:
            continue
        job = jobs[name]
        needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
        assert "changes" in needs, name
        condition = job["if"]
        # "!= 'false'": an empty output (a failed changes job) runs the job.
        assert f"needs.changes.outputs.{output} != 'false'" in condition, name
        assert "!cancelled()" in condition, name
        # A skipped upstream job (not needed by this change) must not skip this one.
        for upstream in needs:
            if upstream != "changes" and name != "e2e-web":
                assert f"needs.{upstream}.result" in condition, (name, upstream)


def test_push_runs_what_it_ran_before(repo_root: Path) -> None:
    jobs = _workflow(repo_root, "ci.yml")["jobs"]
    on_push = {"python", "openfga-model", "docs", "web", "compose", "gradle-wrapper"}
    for name, job in jobs.items():
        pr_only = "github.event_name != 'push'" in str(job.get("if", ""))
        if name == "changes":
            assert "if" not in job
        elif name in on_push:
            assert not pr_only, name
        else:
            assert pr_only, name


def test_ci_config_lints_the_workflows(repo_root: Path) -> None:
    job = _workflow(repo_root, "ci.yml")["jobs"]["ci-config"]
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert '"$RUNNER_TEMP/actionlint"' in runs
    assert "sha256sum -c -" in runs
    assert "shellcheck tools/repo-checks/scripts/*.sh" in runs
    assert "uv run pytest tools/repo-checks" in runs
    assert job["env"]["REPO_CHECKS"] == "${{ needs.changes.outputs.python == 'false' }}"


# --- releases --------------------------------------------------------------------------------


def _dependabot(repo_root: Path) -> list[dict[str, Any]]:
    config = yaml.safe_load((repo_root / ".github" / "dependabot.yml").read_text("utf-8"))
    updates: list[dict[str, Any]] = config["updates"]
    return updates


def test_dependabot_ci_bumps_are_ci_commits(repo_root: Path) -> None:
    prefixes = {
        (u["package-ecosystem"], u["directory"]): u["commit-message"]["prefix"]
        for u in _dependabot(repo_root)
    }
    assert prefixes[("github-actions", "/")] == "ci"
    assert prefixes[("pip", "/.clusterfuzzlite")] == "ci"
    assert prefixes[("devcontainers", "/")] == "ci"
    assert prefixes[("docker", "/.devcontainer")] == "ci"
    # Runtime dependencies still release a patch.
    assert prefixes[("uv", "/")] == "fix(deps)"
    assert prefixes[("npm", "/")] == "fix(deps)"


def _release_rules(repo_root: Path) -> list[dict[str, Any]]:
    policy = yaml.safe_load((repo_root / ".github" / "lktec-bot.yml").read_text("utf-8"))
    rules: list[dict[str, Any]] = policy["modules"]["release"]["rules"]
    return rules


def test_fix_ci_releases_nothing(repo_root: Path) -> None:
    rules = _release_rules(repo_root)
    assert {"type": "fix", "scope": "ci", "release": "none"} in rules
    for rule in rules:
        if rule.get("release") != "none":
            # ci and chore keep the default (no release); deps fixes are not caught by a ci rule.
            assert rule.get("type") not in {"ci", "chore"}, rule
            assert rule.get("scope") not in {"ci"}, rule
    assert not any(rule.get("scope") == "deps" for rule in rules)


def test_release_skips_ci_only_changes(repo_root: Path) -> None:
    steps = _workflow(repo_root, "release.yml")["jobs"]["guard"]["steps"]
    run = next(s["run"] for s in steps if s.get("id") == "guard")
    assert "ci_scope.py release-guard" in run
    assert "git describe --tags --abbrev=0 --match 'v*'" in run
    assert '= "ci_only=true"' in run
    # The guard only ever prevents a release; the error paths fall through to the release.
    guard = run.index("ci_scope.py release-guard")
    assert run.index("skip=true") > guard


# --- the command line against a real repository -----------------------------------------------

GIT = shutil.which("git")


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
    assert GIT is not None
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)  # noqa: S603
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.org", "-c", "commit.gpgsign=false"]
    subprocess.run(  # noqa: S603
        [GIT, *identity, "commit", "-q", "-m", message], cwd=repo, check=True
    )


@pytest.mark.skipif(GIT is None, reason="git not installed")
def test_command_line(
    ci_scope: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert GIT is not None
    repo = tmp_path / "repo"
    subprocess.run([GIT, "init", "-q", str(repo)], check=True)  # noqa: S603
    _commit(repo, {ci_scope.WORKFLOW: BASE, "docs/index.md": "a"}, "base")
    _commit(repo, {".github/dependabot.yml": "x", "docs/index.md": "b"}, "head")
    monkeypatch.chdir(repo)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    assert ci_scope.main(["jobs", "--base", "HEAD~1", "--head", "HEAD"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "docs=true" in lines
    assert "python=false" in lines
    assert "ci_only=false" in lines
    assert len(lines) == len(ci_scope.JOBS) + 1
    assert "| docs | yes | `docs/index.md` |" in summary.read_text("utf-8")

    _commit(repo, {ci_scope.WORKFLOW: BASE.replace("run: build", "run: b2")}, "ci")
    assert ci_scope.main(["jobs", "--base", "HEAD~1", "--head", "HEAD"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line for line in out if line.endswith("=true")] == ["docker=true", "ci_only=true"]

    assert ci_scope.main(["release-guard", "--base", "HEAD~1"]) == 0
    assert capsys.readouterr().out.strip() == "ci_only=true"
    assert ci_scope.main(["release-guard", "--base", "HEAD~2"]) == 0
    assert capsys.readouterr().out.strip() == "ci_only=false"

    assert ci_scope.main(["jobs", "--all"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert all(line.endswith("=true") for line in out if not line.startswith("ci_only"))
