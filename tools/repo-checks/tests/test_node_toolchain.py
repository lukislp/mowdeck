# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""How pnpm and Node are set up (docs/dev/web-app.md, "Node version" and "pnpm and Corepack").

Node 25 and later no longer ship Corepack, so every place that runs ``corepack enable`` first
installs Corepack from npm at the version of ``.corepack-version``, the one place that names it
(``packageManager`` in package.json still pins pnpm and its hash). The job ``web-node26`` of
ci.yml runs the web checks on Node 26 beside the required ``web`` job on ``.node-version``.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

INSTALL_RE = re.compile(
    r"npm install\b[^\n]*corepack@\$\((?:cat|<)\s*(?:/tmp/corepack-version|\.corepack-version)\)"
)
ENABLE_RE = re.compile(r"\bcorepack enable\b")
LITERAL_RE = re.compile(r"corepack@v?\d")
TEXT_SUFFIXES = {".md", ".sh", ".yml", ".yaml", ".json", ".py", ".toml", ".ts", ".js", ".mjs"}
# Prose that records how an earlier version of the workspace was set up.
HISTORICAL = {"docs/dev/design-web-app.md"}
THIS_FILE = "tools/repo-checks/tests/test_node_toolchain.py"


def _tracked(repo_root: Path) -> list[str]:
    git = shutil.which("git")
    if git is None or not (repo_root / ".git").exists():
        pytest.skip("needs a git checkout")
    out = subprocess.run(  # noqa: S603
        [git, "ls-files", "-z"], cwd=repo_root, check=True, capture_output=True, text=True
    ).stdout
    return [name for name in out.split("\0") if name and (repo_root / name).is_file()]


def _is_text(name: str) -> bool:
    path = Path(name)
    return path.suffix in TEXT_SUFFIXES or path.name.startswith("Dockerfile")


def _text(repo_root: Path, name: str) -> str:
    return (repo_root / name).read_text("utf-8")


def test_corepack_version_is_one_exact_release(repo_root: Path) -> None:
    text = (repo_root / ".corepack-version").read_text("utf-8")
    assert re.fullmatch(r"\d+\.\d+\.\d+\n", text), "one exact x.y.z line"


def test_no_other_file_names_a_corepack_version(repo_root: Path) -> None:
    for name in _tracked(repo_root):
        if name in {THIS_FILE, "pnpm-lock.yaml"} or not _is_text(name):
            continue
        assert not LITERAL_RE.search(_text(repo_root, name)), (
            f"{name}: read .corepack-version instead of naming a Corepack version"
        )


def test_corepack_enable_follows_the_pinned_install(repo_root: Path) -> None:
    runners = [
        name
        for name in _tracked(repo_root)
        if (Path(name).name.startswith("Dockerfile") or name.endswith(".sh"))
        and ENABLE_RE.search(_text(repo_root, name))
    ]
    assert {
        "apps/web/Dockerfile",
        "services/matter-server/Dockerfile",
        ".devcontainer/on-create.sh",
    } <= set(runners)
    for name in runners:
        text = _text(repo_root, name)
        install = INSTALL_RE.search(text)
        assert install, f"{name}: install Corepack from .corepack-version before enabling it"
        assert install.start() < ENABLE_RE.search(text).start(), name  # type: ignore[union-attr]
        if Path(name).name == "Dockerfile":
            assert "COPY .corepack-version /tmp/corepack-version" in text, name
            ignore = _text(repo_root, f"{name}.dockerignore")
            assert "!.corepack-version" in ignore.splitlines(), f"{name}.dockerignore"


def test_documents_point_to_one_corepack_instruction(repo_root: Path) -> None:
    for name in _tracked(repo_root):
        if not name.endswith(".md") or name in HISTORICAL or name.startswith("docs/adr/"):
            continue
        if ENABLE_RE.search(_text(repo_root, name)):
            assert name == "docs/dev/web-app.md", f"{name}: link to 'pnpm and Corepack' instead"


def test_node26_job_runs_beside_the_required_web_job(repo_root: Path) -> None:
    workflow: dict[str, Any] = yaml.safe_load(
        (repo_root / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    jobs = workflow["jobs"]

    def setup_node(job: str) -> dict[str, Any]:
        steps = [
            s
            for s in jobs[job]["steps"]
            if str(s.get("uses", "")).startswith("actions/setup-node@")
        ]
        assert len(steps) == 1, job
        with_: dict[str, Any] = steps[0]["with"]
        return with_

    assert "strategy" not in jobs["web"], "a matrix would rename the required check `web`"
    assert "name" not in jobs["web"]
    assert setup_node("web")["node-version-file"] == ".node-version"
    node26 = setup_node("web-node26")
    assert re.fullmatch(r"26\.\d+\.\d+", str(node26["node-version"])), "an exact 26.x release"
    assert "node-version-file" not in node26
    assert "needs.changes.outputs.web != 'false'" in jobs["web-node26"]["if"]
    assert "github.event_name != 'push'" in jobs["web-node26"]["if"]
    web_steps = [s.get("run") for s in jobs["web"]["steps"] if "run" in s]
    node26_steps = [s.get("run") for s in jobs["web-node26"]["steps"] if "run" in s]
    for command in (
        "pnpm install --frozen-lockfile",
        "pnpm -r lint",
        "pnpm -r check",
        "pnpm --filter web build",
    ):
        assert command in web_steps
        assert command in node26_steps
