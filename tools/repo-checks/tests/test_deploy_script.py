"""``infra/compose/deploy.sh``: the documented release update with the maintenance window.

The script runs against fake ``docker`` and ``git`` commands and a stub ``verify-release.sh``
that log their calls, so the order of the steps, the fail-closed behaviour and the window around
the restart are tested without a stack (ADR 0055).
"""

import gzip
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest


def _find_bash() -> str | None:
    """bash from PATH; on Windows Git Bash (the bash.exe of WSL cannot run these scripts)."""
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if os.name == "nt" and git_bash.exists():
        return str(git_bash)
    return shutil.which("bash")


BASH = _find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not installed")

SECRET = "super-secret-db-password-4711"  # noqa: S105

FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
case "$*" in
  *"ps --status running --quiet postgres"*) echo abc123 ;;
  *pg_dumpall*)
    echo "-- dump"
    [ "${FAKE_DUMP:-ok}" = ok ] && echo "-- PostgreSQL database cluster dump complete"
    exit 0
    ;;
  *"maintenance open"*) [ "${FAKE_NO_WINDOW:-}" = 1 ] && exit 1; echo "maintenance: open" ;;
  *"maintenance close"*) echo "maintenance: closed" ;;
  *"ps -a --format"*)
    printf 'postgres|running|healthy|0\\nnotifications-migrate|exited||0\\n'
    [ "${FAKE_BAD:-}" = 1 ] && echo 'fleet|running|unhealthy|0'
    exit 0
    ;;
  *" up "*) [ "${FAKE_UP_FAIL:-}" = 1 ] && exit 1 ;;
esac
exit 0
"""

FAKE_GIT = """#!/usr/bin/env bash
echo "git $*" >> "$FAKE_LOG"
case "$*" in
  *"status --porcelain"*) printf '%s' "${FAKE_DIRTY:-}" ;;
esac
exit 0
"""

STUB_VERIFY = """#!/usr/bin/env bash
echo "verify-release $*" >> "$FAKE_LOG"
exit "${FAKE_VERIFY_EXIT:-0}"
"""


@dataclass
class Stack:
    """A copy of the script next to fakes, in a temporary directory."""

    root: Path
    env: dict[str, str] = field(default_factory=dict)

    @property
    def log(self) -> list[str]:
        path = self.root / "calls.log"
        return path.read_text("utf-8").splitlines() if path.exists() else []

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        assert BASH is not None
        merged = {
            **os.environ,
            "FAKE_BIN": (self.root / "bin").as_posix(),
            "BASH_ENV": (self.root / "env.sh").as_posix(),
            "FAKE_LOG": (self.root / "calls.log").as_posix(),
            "MOWDECK_BACKUP_DIR": self.backups.as_posix(),
            "HOME": (self.root / "home").as_posix(),
            **self.env,
            **env,
        }
        return subprocess.run(  # noqa: S603
            [BASH, (self.root / "stack" / "deploy.sh").as_posix(), *args],
            capture_output=True,
            text=True,
            check=False,
            env=merged,
            timeout=240,
        )

    def calls(self, needle: str) -> list[int]:
        """Indexes of the logged calls that contain ``needle``."""
        return [i for i, line in enumerate(self.log) if needle in line]


@pytest.fixture
def stack(repo_root: Path, tmp_path: Path) -> Stack:
    stack_dir = tmp_path / "stack"
    bin_dir = tmp_path / "bin"
    stack_dir.mkdir()
    bin_dir.mkdir()
    (tmp_path / "home").mkdir()
    # bash sources this first: the fakes win over a login profile that reorders PATH (Git Bash)
    (tmp_path / "env.sh").write_text(
        'PATH="$(cygpath -u "$FAKE_BIN" 2>/dev/null || echo "$FAKE_BIN"):$PATH"\n',
        encoding="utf-8",
        newline="\n",
    )
    shutil.copy(repo_root / "infra" / "compose" / "deploy.sh", stack_dir / "deploy.sh")
    (stack_dir / ".env").write_text(
        f"POSTGRES_PASSWORD={SECRET}\nMOWDECK_VERSION=0.1.0\n", encoding="utf-8", newline="\n"
    )
    for directory, name, text in (
        (stack_dir, "verify-release.sh", STUB_VERIFY),
        (bin_dir, "docker", FAKE_DOCKER),
        (bin_dir, "git", FAKE_GIT),
    ):
        path = directory / name
        path.write_text(text, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    return Stack(tmp_path)


def test_deploy_runs_the_documented_procedure_in_order(stack: Stack) -> None:
    result = stack.run("0.2.0")
    assert result.returncode == 0, result.stderr
    order = [
        stack.calls("git fetch --tags"),
        stack.calls("pg_dumpall"),
        stack.calls("git checkout --detach v0.2.0"),
        stack.calls("verify-release 0.2.0"),
        stack.calls(" pull"),
        stack.calls("maintenance open --minutes 15 --reason deploy 0.2.0"),
        stack.calls(" up -d --no-build --wait"),
        stack.calls("maintenance close"),
    ]
    assert all(len(found) == 1 for found in order), stack.log
    indexes = [found[0] for found in order]
    assert indexes == sorted(indexes), stack.log
    assert "MOWDECK_VERSION=0.2.0" in (stack.root / "stack" / ".env").read_text("utf-8")
    assert "-f docker-compose.yml -f docker-compose.release.yml" in stack.log[indexes[4]]
    assert "MowDeck v0.2.0 is running" in result.stdout


def test_deploy_keeps_a_complete_backup_and_never_prints_secrets(stack: Stack) -> None:
    result = stack.run("v0.2.0")  # a leading v is accepted
    assert result.returncode == 0, result.stderr
    dumps = list(stack.backups.glob("mowdeck-before-0.2.0-*.sql.gz"))
    envs = list(stack.backups.glob("env-before-0.2.0-*.env"))
    assert len(dumps) == 1
    assert len(envs) == 1
    assert "cluster dump complete" in gzip.decompress(dumps[0].read_bytes()).decode()
    assert SECRET in envs[0].read_text("utf-8")  # the copy is the point of the backup ...
    assert SECRET not in result.stdout + result.stderr  # ... but nothing prints it
    assert SECRET not in "\n".join(stack.log)


def test_deploy_refuses_when_verify_release_fails(stack: Stack) -> None:
    result = stack.run("0.2.0", FAKE_VERIFY_EXIT="1")
    assert result.returncode != 0
    assert not stack.calls(" pull")
    assert not stack.calls(" up ")
    assert not stack.calls("maintenance")
    assert "MOWDECK_VERSION=0.1.0" in (stack.root / "stack" / ".env").read_text("utf-8")


def test_deploy_refuses_an_incomplete_backup(stack: Stack) -> None:
    result = stack.run("0.2.0", FAKE_DUMP="truncated")
    assert result.returncode != 0
    assert "incomplete" in result.stderr
    assert not stack.calls("git checkout")
    assert not stack.calls(" up ")


def test_deploy_refuses_a_dirty_checkout(stack: Stack) -> None:
    result = stack.run("0.2.0", FAKE_DIRTY=" M infra/compose/docker-compose.yml")
    assert result.returncode != 0
    assert "local changes" in result.stderr
    assert not stack.calls("git checkout")


def test_deploy_closes_the_window_when_up_fails(stack: Stack) -> None:
    result = stack.run("0.2.0", FAKE_UP_FAIL="1")
    assert result.returncode != 0
    assert len(stack.calls("maintenance open")) == 1
    assert len(stack.calls("maintenance close")) == 1
    assert stack.calls("maintenance close")[0] > stack.calls(" up -d")[0]


def test_deploy_goes_on_without_a_window_the_old_image_cannot_open(stack: Stack) -> None:
    result = stack.run("0.2.0", FAKE_NO_WINDOW="1")
    assert result.returncode == 0, result.stderr
    assert "no maintenance window" in result.stderr
    assert not stack.calls("maintenance close")
    assert stack.calls(" up -d")


def test_deploy_without_maintenance_and_with_a_custom_length(stack: Stack) -> None:
    assert stack.run("--no-maintenance", "0.2.0").returncode == 0
    assert not stack.calls("maintenance")
    (stack.root / "calls.log").unlink()
    assert stack.run("--window-minutes", "30", "0.2.0").returncode == 0
    assert stack.calls("maintenance open --minutes 30")


def test_deploy_fails_when_a_service_is_not_healthy_after_up(stack: Stack) -> None:
    result = stack.run("0.2.0", FAKE_BAD="1")
    assert result.returncode != 0
    assert "fleet" in result.stdout
    assert "not running and healthy" in result.stderr
    assert stack.calls("maintenance close")  # closed before the summary


def test_deploy_dry_run_changes_nothing(stack: Stack) -> None:
    result = stack.run("--dry-run", "0.2.0")
    assert result.returncode == 0, result.stderr
    assert "[dry-run]" in result.stdout
    assert stack.log == []
    assert not stack.backups.exists()
    assert "MOWDECK_VERSION=0.1.0" in (stack.root / "stack" / ".env").read_text("utf-8")


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--bogus", "0.2.0"),
        ("0.2.0", "0.3.0"),
        ("latest",),
        ("0.2",),
        ("--window-minutes", "0", "0.2.0"),
    ],
)
def test_deploy_rejects_bad_arguments(stack: Stack, args: tuple[str, ...]) -> None:
    result = stack.run(*args)
    assert result.returncode != 0
    assert stack.log == []


def test_deploy_script_is_lf_executable_and_shellcheck_clean(repo_root: Path) -> None:
    path = repo_root / "infra" / "compose" / "deploy.sh"
    assert b"\r" not in path.read_bytes()
    if shutil.which("git") and (repo_root / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "--stage", "infra/compose/deploy.sh"],  # noqa: S607
            capture_output=True,
            text=True,
            cwd=repo_root,
            check=False,
        )
        assert result.stdout.startswith("100755"), "git must keep the executable bit"
    if shutil.which("shellcheck") is not None:
        lint = subprocess.run(  # noqa: S603
            ["shellcheck", str(path)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        assert lint.returncode == 0, lint.stdout
