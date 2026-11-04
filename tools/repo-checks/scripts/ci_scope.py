# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Which jobs of ci.yml a pull request needs, and whether a change set is CI only.

Two users, both on the runner's ``python3`` (3.12 or newer):

* the job ``changes`` of ``.github/workflows/ci.yml`` runs ``jobs`` on a pull request. It lists
  the files the pull request changes (``git diff`` between the merge commit and its first
  parent, the base branch), maps every path to the jobs that check it (``RULES``, first match
  wins) and prints one ``<job>=true|false`` line per entry of ``JOBS`` for ``$GITHUB_OUTPUT``.
  A path no rule knows runs every job (fail safe). When ``ci.yml`` itself changes, the jobs
  whose definition changed run as well (``changed_workflow_jobs``; PyYAML resolves the anchors,
  so a changed anchor counts for every job that uses it), and a change outside ``jobs`` (``on``,
  ``env``, ``permissions``, ...) runs everything. On a push or by hand (``--all``) every job
  runs, as before.
* the job ``version`` of ``.github/workflows/release.yml`` runs ``release-guard`` with the last
  release tag: when every file changed since then is CI configuration (``CI_PATTERNS``), it
  prints ``ci_only=true`` and no release is published, whatever the commit types say.

Jobs skipped this way report "skipped", which a required status check accepts; the workflow
itself always starts, so no required check is left waiting (docs/dev/ci.md).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase
from typing import Any

JOBS = (
    "python",
    "openfga_model",
    "docs",
    "web",
    "compose",
    "gradle_wrapper",
    "helm",
    "docker",
    "camera_relay_media",
    "e2e",
    "e2e_web",
    "e2e_demo",
)
"""The outputs of the job ``changes``: one per scoped job of ci.yml (``e2e_web`` covers the
shards and the merging job ``e2e-web``)."""

ALL = frozenset(JOBS)
NONE: frozenset[str] = frozenset()
STACK = frozenset({"docker", "camera_relay_media", "e2e", "e2e_web", "e2e_demo"})
"""The image builds and every job that starts the Compose stack from them."""
SHARED = ALL - {"web", "gradle_wrapper"}
"""Inputs every Python, image and stack job reads (lock file, Compose, base images)."""

WORKFLOW = ".github/workflows/ci.yml"

WORKFLOW_JOBS: dict[str, str | None] = {
    "changes": None,
    "ci-config": None,
    "python": "python",
    "openfga-model": "openfga_model",
    "docs": "docs",
    "web": "web",
    "compose": "compose",
    "gradle-wrapper": "gradle_wrapper",
    "helm": "helm",
    "docker": "docker",
    "camera-relay-media": "camera_relay_media",
    "e2e": "e2e",
    "e2e-web-shard": "e2e_web",
    "e2e-web": "e2e_web",
    "e2e-demo": "e2e_demo",
}
"""Every job of ci.yml and its output (None: always runs). A job missing here runs everything
when its definition changes; tools/repo-checks/tests/test_ci_scope.py keeps the list complete."""

CI_SCRIPTS = (
    "tools/repo-checks/scripts/ci_scope.py",
    "tools/repo-checks/scripts/compare_builds.py",
    "tools/repo-checks/scripts/cosign_retry.sh",
    "tools/repo-checks/scripts/coverage_summary.py",
    "tools/repo-checks/scripts/gh_retry.sh",
    "tools/repo-checks/scripts/licence_gate.py",
    "tools/repo-checks/scripts/release_assets.py",
    "tools/repo-checks/scripts/reproducible_build.sh",
    "tools/repo-checks/scripts/source_offer.py",
    "tools/repo-checks/scripts/vendor_lib_guard.py",
)
"""Scripts only workflows run; their tests are repo-checks tests (job ci-config)."""

CI_PATTERNS = (
    ".github/*",
    ".releaserc.json",
    "socket.yml",
    "osv-scanner.toml",
    ".pre-commit-config.yaml",
    ".clusterfuzzlite/*",
    "tools/repo-checks/tests/*",
    "tools/repo-checks/README.md",
    "tools/repo-checks/pyproject.toml",
    *CI_SCRIPTS,
)
"""CI configuration: workflows, Dependabot, the release and scanner configuration, the
ClusterFuzzLite build (fuzzing.yml runs on it by its own paths filter) and the repo-checks
tests and CI scripts. Nothing here ends up in an image, the chart or an app."""

RULES: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = (
    # repo-checks scripts that product checks run, before the CI patterns.
    (("tools/repo-checks/scripts/export_openapi.py",), frozenset({"python", "docs", "web"})),
    (
        (
            "tools/repo-checks/scripts/api_reference.py",
            "tools/repo-checks/scripts/arch_diagrams.py",
            "tools/repo-checks/scripts/capability_matrix.py",
        ),
        frozenset({"docs"}),
    ),
    # Renders the NATS users of Compose and the chart.
    (("tools/repo-checks/scripts/nats_auth.py",), SHARED),
    (("tools/repo-checks/tests/test_helm.py",), frozenset({"helm"})),
    # ci.yml: the jobs whose definition changed are added by changed_workflow_jobs.
    (CI_PATTERNS, NONE),
    (("tools/wiki-sync/*",), frozenset({"python", "docs"})),
    (("tools/ts-codegen/*",), frozenset({"web"}) | STACK),
    (
        (
            "docs/*",
            "mkdocs.yml",
            "README.md",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "SECURITY.md",
            "GOVERNANCE.md",
            "ROADMAP.md",
        ),
        frozenset({"docs"}),
    ),
    # Copied into every image.
    (("LICENSE", "LICENSING.md", "LICENSES/*"), frozenset({"docs"}) | STACK),
    (("pyproject.toml", "uv.lock", ".python-version"), SHARED),
    (("libs/mowdeck-ui/*",), frozenset({"web"}) | STACK),
    # mowdeck-authz packages the OpenFGA model; the providers feed the capability matrix.
    (("libs/*",), frozenset({"python", "docs", "openfga_model"}) | STACK),
    (("services/matter-server/*",), frozenset({"web"}) | STACK),
    (("services/*",), frozenset({"python", "docs"}) | STACK),
    (("integrations/*", "fuzz/*", "templates/*"), frozenset({"python"})),
    # The committed OpenAPI documents: openapi-check (python), the API reference (docs), the types.
    (("apps/web/openapi/*",), frozenset({"python", "docs", "web"}) | STACK),
    (("apps/web/*",), frozenset({"web"}) | STACK),
    (
        ("apps/mobile/android/gradle/*", "apps/mobile/android/gradlew*"),
        frozenset({"web", "gradle_wrapper"}),
    ),
    (("apps/*",), frozenset({"web"})),
    (("tests/e2e/*",), frozenset({"python", "e2e"})),
    # stack.env and demo.env also configure the Compose checks.
    (("tests/e2e-web/*",), frozenset({"web", "compose", "e2e_web", "e2e_demo"})),
    (("tests/load/*",), frozenset({"compose"})),
    (("infra/helm/*",), frozenset({"helm"})),
    # Compose, the base images (.env.example, mirror-env.sh), OpenFGA, billing, entitlements.
    (("infra/*",), SHARED),
    (
        (
            "package.json",
            "pnpm-lock.yaml",
            "pnpm-workspace.yaml",
            ".npmrc",
            ".node-version",
            "eslint.config.js",
            "stylelint.config.js",
            ".prettierrc.json",
            ".prettierignore",
        ),
        frozenset({"web"}) | STACK,
    ),
    # REUSE.toml is checked by `reuse lint` in ci-config, which runs on every pull request.
    ((".gitignore", ".editorconfig", "REUSE.toml"), NONE),
)
"""Path patterns (fnmatch, ``*`` also matches ``/``) and the jobs they need; the first matching
rule wins and a path without one needs every job."""


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def jobs_for_path(path: str) -> frozenset[str]:
    """The jobs a change of ``path`` needs (every job for an unknown path)."""
    for patterns, jobs in RULES:
        if _matches(path, patterns):
            return jobs
    return ALL


def is_ci_path(path: str) -> bool:
    """Whether ``path`` is CI configuration that ships nothing."""
    return _matches(path, CI_PATTERNS)


def ci_only(paths: Sequence[str]) -> bool:
    """Whether a non-empty change set touches nothing but CI configuration."""
    return bool(paths) and all(is_ci_path(path) for path in paths)


def changed_workflow_jobs(base: str | None, head: str | None) -> frozenset[str]:
    """The outputs whose ci.yml job changed between two versions of the file.

    Anything that cannot be compared job by job (a new or deleted file, a parse error, PyYAML
    missing, a change outside ``jobs``, a job not in ``WORKFLOW_JOBS``) needs every job.
    """
    if base is None or head is None:
        return ALL
    try:
        import yaml  # only this comparison needs PyYAML; release-guard runs without it
    except ImportError:
        return ALL
    try:
        old: Any = yaml.safe_load(base)
        new: Any = yaml.safe_load(head)
    except yaml.YAMLError:
        return ALL
    if not isinstance(old, dict) or not isinstance(new, dict):
        return ALL
    old_jobs = old.get("jobs")
    new_jobs = new.get("jobs")
    if not isinstance(old_jobs, dict) or not isinstance(new_jobs, dict):
        return ALL
    if {k: v for k, v in old.items() if k != "jobs"} != {
        k: v for k, v in new.items() if k != "jobs"
    }:
        return ALL
    needed: set[str] = set()
    for name, job in new_jobs.items():
        if old_jobs.get(name) == job:
            continue
        if name not in WORKFLOW_JOBS:
            return ALL
        output = WORKFLOW_JOBS[name]
        if output is not None:
            needed.add(output)
    return frozenset(needed)


def scope(
    paths: Sequence[str], workflow_base: str | None = None, workflow_head: str | None = None
) -> dict[str, list[str]]:
    """Every job of ``JOBS`` with the changed paths that need it (empty: the job is skipped)."""
    reasons: dict[str, list[str]] = {job: [] for job in JOBS}
    for path in paths:
        for job in jobs_for_path(path):
            reasons[job].append(path)
        if path == WORKFLOW:
            for job in changed_workflow_jobs(workflow_base, workflow_head):
                reasons[job].append(f"{WORKFLOW} (job definition)")
    return reasons


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    ).stdout


def changed_paths(base: str, head: str) -> list[str]:
    """The files changed between two revisions; a rename lists the old and the new path."""
    out = _git("diff", "--name-only", "--no-renames", "-z", base, head)
    return [path for path in out.split("\0") if path]


def _show(rev: str, path: str) -> str | None:
    try:
        return _git("show", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return None


def _summary(lines: list[str]) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def _jobs_command(args: argparse.Namespace) -> int:
    if args.all:
        reasons = {job: ["push or manual run: every job"] for job in JOBS}
        paths: list[str] = []
    else:
        paths = changed_paths(args.base, args.head)
        base = head = None
        if WORKFLOW in paths:
            base = _show(args.base, WORKFLOW)
            head = _show(args.head, WORKFLOW)
        reasons = scope(paths, base, head)
    for job in JOBS:
        print(f"{job}={'true' if reasons[job] else 'false'}")
    print(f"ci_only={'true' if ci_only(paths) else 'false'}")
    lines = ["## CI scope", ""]
    if not args.all:
        lines.append(f"{len(paths)} changed file(s); CI configuration only: {ci_only(paths)}.")
        lines.append("")
    lines += ["| Job | Runs | Because of |", "| --- | --- | --- |"]
    for job in JOBS:
        why = reasons[job]
        shown = ", ".join(f"`{p}`" for p in why[:3])
        if len(why) > 3:
            shown += f" and {len(why) - 3} more"
        lines.append(f"| {job} | {'yes' if why else 'skipped'} | {shown} |")
    _summary(lines)
    return 0


def _release_guard_command(args: argparse.Namespace) -> int:
    paths = changed_paths(args.base, args.head)
    print(f"ci_only={'true' if ci_only(paths) else 'false'}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    jobs = commands.add_parser("jobs", help="print <job>=true|false for $GITHUB_OUTPUT")
    jobs.add_argument("--all", action="store_true", help="every job (push, manual run)")
    jobs.add_argument("--base", default="HEAD^1")
    jobs.add_argument("--head", default="HEAD")
    guard = commands.add_parser("release-guard", help="print ci_only=true|false")
    guard.add_argument("--base", required=True)
    guard.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    if args.command == "jobs":
        return _jobs_command(args)
    return _release_guard_command(args)


if __name__ == "__main__":
    sys.exit(main())
