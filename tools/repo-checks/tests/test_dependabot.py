"""Dependabot reaches every workspace member, and vendor library updates wait for the owner.

* the uv workspace globs end without "/" (Dependabot's uv fetcher skips members otherwise, so
  their dependencies, pymammotion among them, were never updated);
* the uv entry ignores the workspace members' own names (Dependabot looks them up on PyPI and
  fails the run), but never a vendor library, and does not group them;
* every vendor library of an adapter (``[tool.mowdeck.adapter] vendor-modules``) is listed in
  ``VENDOR_LIBS`` of ``scripts/vendor_lib_guard.py``, and that list is the auto-merge exclusion;
* the guard script finds updates in a lock file pair and fails until the label is removed;
* the guard and label workflows run on the right events with the least permissions.

Policy: docs/ops/vendor-lib-updates.md.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(".github") / "workflows"


@pytest.fixture(scope="module")
def guard(repo_root: Path) -> ModuleType:
    """The guard script as a module."""
    path = repo_root / "tools" / "repo-checks" / "scripts" / "vendor_lib_guard.py"
    spec = importlib.util.spec_from_file_location("vendor_lib_guard", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _uv_entry(repo_root: Path) -> dict[str, Any]:
    config = yaml.safe_load((repo_root / ".github" / "dependabot.yml").read_text("utf-8"))
    entries = [u for u in config["updates"] if u["package-ecosystem"] == "uv"]
    assert len(entries) == 1
    entry: dict[str, Any] = entries[0]
    return entry


def _workflow(repo_root: Path, name: str) -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load((repo_root / WORKFLOWS / name).read_text("utf-8"))
    return data


def _on(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML reads the key "on" as the boolean True.
    on = workflow["on"] if "on" in workflow else workflow[True]
    assert isinstance(on, dict)
    return on


def _root(repo_root: Path) -> dict[str, Any]:
    data: dict[str, Any] = tomllib.loads((repo_root / "pyproject.toml").read_text("utf-8"))
    return data


def _member_names(repo_root: Path) -> set[str]:
    workspace = _root(repo_root)["tool"]["uv"]["workspace"]
    excluded = {e.rstrip("/") for e in workspace.get("exclude", [])}
    names = set()
    for pattern in workspace["members"]:
        for path in sorted(repo_root.glob(pattern)):
            relative = path.relative_to(repo_root).as_posix()
            if path.is_dir() and relative not in excluded:
                project = tomllib.loads((path / "pyproject.toml").read_text("utf-8"))
                names.add(project["project"]["name"])
    return names


def _adapter_vendor_modules(repo_root: Path) -> set[str]:
    modules = set()
    for pyproject in sorted((repo_root / "libs").glob("mowdeck-control-*/pyproject.toml")):
        data = tomllib.loads(pyproject.read_text("utf-8"))
        modules.update(data["tool"]["mowdeck"]["adapter"]["vendor-modules"])
    return modules


def test_workspace_globs_have_no_trailing_slash(repo_root: Path) -> None:
    workspace = _root(repo_root)["tool"]["uv"]["workspace"]
    for pattern in [*workspace["members"], *workspace.get("exclude", [])]:
        assert not pattern.endswith("/"), f"{pattern!r}: Dependabot skips such members"


def test_uv_entry_ignores_every_workspace_member(repo_root: Path) -> None:
    ignored = [i["dependency-name"] for i in _uv_entry(repo_root).get("ignore", [])]
    members = _member_names(repo_root)
    assert len(members) > 10
    missing = sorted(m for m in members if not any(fnmatch.fnmatch(m, i) for i in ignored))
    assert not missing, f"add to the uv ignore list of .github/dependabot.yml: {missing}"
    stale = [i for i in ignored if not any(fnmatch.fnmatch(m, i) for m in members)]
    assert not stale, f"no workspace member matches: {stale}"


def test_vendor_libs_are_updated_one_pr_each(repo_root: Path, guard: ModuleType) -> None:
    entry = _uv_entry(repo_root)
    ignored = [i["dependency-name"] for i in entry.get("ignore", [])]
    for lib in guard.VENDOR_LIBS:
        assert not any(fnmatch.fnmatch(lib, i) for i in ignored), f"{lib} must not be ignored"
    assert "allow" not in entry  # an allow list would have to name every routine dependency
    for group in (entry.get("groups") or {}).values():
        patterns = group.get("patterns", [])
        excluded = group.get("exclude-patterns", [])
        for lib in guard.VENDOR_LIBS:
            grouped = any(fnmatch.fnmatch(lib, p) for p in patterns)
            assert not grouped or any(fnmatch.fnmatch(lib, p) for p in excluded), lib
    # Every vendor PR can stay open until its test and routine updates still get through.
    assert entry["open-pull-requests-limit"] >= len(guard.VENDOR_LIBS) + 2


def test_every_adapter_vendor_module_is_a_vendor_lib(repo_root: Path, guard: ModuleType) -> None:
    modules = _adapter_vendor_modules(repo_root)
    assert "pymammotion" in modules
    assert modules <= set(guard.VENDOR_LIBS), sorted(modules - set(guard.VENDOR_LIBS))


def test_auto_merge_excludes_exactly_the_vendor_libs(repo_root: Path, guard: ModuleType) -> None:
    workflow = _workflow(repo_root, "dependabot-auto-merge.yml")
    raw = workflow["jobs"]["dependabot"]["with"]["exclude-dependencies"]
    excluded = [name.strip() for name in raw.replace(",", " ").split()]
    assert sorted(excluded) == sorted(guard.VENDOR_LIBS)


def test_guard_runs_on_label_changes_with_read_only_token(repo_root: Path) -> None:
    workflow = _workflow(repo_root, "vendor-lib-guard.yml")
    on = _on(workflow)
    assert set(on) == {"pull_request"}  # never pull_request_target: it runs the PR's script
    assert {"opened", "synchronize", "reopened", "labeled", "unlabeled"} <= set(
        on["pull_request"]["types"]
    )
    assert "paths" not in on["pull_request"]  # a required check must report on every PR
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["hardware-test"]
    assert job["name"] == "vendor-lib / hardware test"
    assert "vendor_lib_guard.py check" in job["steps"][-1]["run"]


def test_labeler_runs_no_pr_code(repo_root: Path) -> None:
    workflow = _workflow(repo_root, "vendor-lib-label.yml")
    on = _on(workflow)
    assert set(on) == {"pull_request_target"}
    # Only when opened: a rebase or branch update must not put the label back after the test.
    assert sorted(on["pull_request_target"]["types"]) == ["opened", "reopened"]
    assert workflow["permissions"] == {}
    job = workflow["jobs"]["label"]
    assert job["permissions"] == {
        "contents": "read",
        "issues": "write",
        "pull-requests": "write",
    }
    for step in job["steps"]:
        # The checkout stays the base branch: no ref/repository of the PR.
        assert "with" not in step or not {"ref", "repository"} & set(step["with"]), step
    run = job["steps"][-1]["run"]
    assert "git show FETCH_HEAD:uv.lock" in run
    assert "vendor_lib_guard.py changes" in run


def _lock(**versions: str | tuple[str, ...]) -> str:
    lines = ['version = 1\nrequires-python = ">=3.13"\n']
    for name, version in versions.items():
        for v in (version,) if isinstance(version, str) else version:
            lines.append(f'[[package]]\nname = "{name}"\nversion = "{v}"\n')
    return "\n".join(lines)


def test_updates_are_version_changes_of_vendor_libs(guard: ModuleType) -> None:
    base = _lock(pymammotion="0.10.9", aioautomower="3.1.0", httpx="0.28.1")
    head = _lock(pymammotion="0.10.13", aioautomower="3.1.0", httpx="0.29.0", pyworxcloud="6.4.2")
    updates = guard.vendor_updates(base, head)
    # A new library (pyworxcloud: a new adapter) and routine packages are no update.
    assert [u.describe() for u in updates] == ["pymammotion 0.10.9 -> 0.10.13"]
    assert guard.vendor_updates(base, base) == []


def test_names_are_normalised_and_forks_compared(guard: ModuleType) -> None:
    base = _lock(PyMammotion=("0.10.9",))
    head = _lock(pymammotion=("0.10.9", "0.10.13"))
    (update,) = guard.vendor_updates(base, head)
    assert update.name == "pymammotion"
    assert update.new == ("0.10.13", "0.10.9")


@pytest.mark.parametrize(
    ("labels", "ok"),
    [
        (["vendor-lib", "needs-hardware-test"], False),
        (["needs-hardware-test"], False),
        ([], False),  # not labelled yet (the labeler runs at the same time): still blocked
        (["vendor-lib"], True),  # the owner removed needs-hardware-test after the test
        (["vendor-lib", "dependencies", "python:uv"], True),
    ],
)
def test_verdict_blocks_until_the_label_is_removed(
    guard: ModuleType, labels: list[str], ok: bool
) -> None:
    update = guard.Update("aioautomower", ("3.1.0",), ("3.2.0",))
    passed, summary = guard.verdict([update], labels)
    assert passed is ok
    assert "aioautomower 3.1.0 -> 3.2.0" in summary
    if not ok:
        assert "docs/ops/vendor-lib-updates.md" in summary
        assert "needs-hardware-test" in summary


def test_verdict_passes_without_vendor_update(guard: ModuleType) -> None:
    passed, _ = guard.verdict([], ["needs-hardware-test"])
    assert passed


def test_check_command_exit_code_and_summary(
    guard: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base.lock"
    head = tmp_path / "head.lock"
    base.write_text(_lock(pymammotion="0.10.9"), "utf-8")
    head.write_text(_lock(pymammotion="0.10.13"), "utf-8")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    args = ["check", "--base", str(base), "--head", str(head)]
    assert guard.main([*args, "--labels", '["vendor-lib","needs-hardware-test"]']) == 1
    assert "pymammotion 0.10.9 -> 0.10.13" in summary.read_text("utf-8")
    assert guard.main([*args, "--labels", '["vendor-lib"]']) == 0
    assert guard.main(["check", "--base", str(base), "--head", str(base)]) == 0
    assert guard.main(["changes", "--base", str(base), "--head", str(head)]) == 0


def test_the_real_lock_has_every_present_vendor_lib(repo_root: Path, guard: ModuleType) -> None:
    lock = (repo_root / "uv.lock").read_text("utf-8")
    locked = guard.locked_versions(lock)
    for module in _adapter_vendor_modules(repo_root):
        assert module in locked, f"{module} is not in uv.lock under that name"
