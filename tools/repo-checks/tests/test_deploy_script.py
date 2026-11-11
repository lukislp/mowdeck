# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

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
    [ "${FAKE_DUMP:-ok}" = fail ] && exit 1
    [ "${FAKE_DUMP:-ok}" = empty ] && exit 0
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
  *"ps --status running --quiet nats"*) [ "${FAKE_NATS_STOPPED:-}" = 1 ] || echo nats123 ;;
  *" logs "*" nats"*)
    case "${FAKE_NATS_RELOAD:-ok}" in
      ok) echo "nats-1  | [INF] Reloaded server configuration (sha256:abc)" ;;
      fail) echo "nats-1  | [ERR] Failed to reload server configuration: parse error" ;;
    esac
    ;;
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

    @property
    def env_backups(self) -> Path:
        return self.root / "env-backups"

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        assert BASH is not None
        merged = {
            **os.environ,
            "FAKE_BIN": (self.root / "bin").as_posix(),
            "BASH_ENV": (self.root / "env.sh").as_posix(),
            "FAKE_LOG": (self.root / "calls.log").as_posix(),
            "MOWDECK_BACKUP_DIR": self.backups.as_posix(),
            "MOWDECK_ENV_BACKUP_DIR": self.env_backups.as_posix(),
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
    for script in ("deploy.sh", "backup-lib.sh"):
        shutil.copy(repo_root / "infra" / "compose" / script, stack_dir / script)
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
    envs = list(stack.env_backups.glob("env-before-0.2.0-*.env"))
    assert len(dumps) == 1
    assert len(envs) == 1
    assert "cluster dump complete" in gzip.decompress(dumps[0].read_bytes()).decode()
    assert SECRET in envs[0].read_text("utf-8")  # the copy is the point of the backup ...
    assert not list(stack.backups.glob("*.env"))  # ... but never beside the dump
    assert SECRET not in result.stdout + result.stderr  # ... but nothing prints it
    assert SECRET not in "\n".join(stack.log)


def test_deploy_refuses_when_verify_release_fails(stack: Stack) -> None:
    result = stack.run("0.2.0", FAKE_VERIFY_EXIT="1")
    assert result.returncode != 0
    assert not stack.calls(" pull")
    assert not stack.calls(" up ")
    assert not stack.calls("maintenance")
    assert "MOWDECK_VERSION=0.1.0" in (stack.root / "stack" / ".env").read_text("utf-8")


STUB_NATS_PASSWORDS = """#!/usr/bin/env bash
echo "nats-passwords $*" >> "$FAKE_LOG"
exit "${FAKE_NATS_EXIT:-0}"
"""


def test_deploy_checks_the_nats_passwords_of_the_release(stack: Stack) -> None:
    """ADR 0074: after the checkout (the release's own check) and before anything changes."""
    path = stack.root / "stack" / "nats-passwords.sh"
    path.write_text(STUB_NATS_PASSWORDS, encoding="utf-8", newline="\n")
    result = stack.run("0.2.0")
    assert result.returncode == 0, result.stderr
    checks = stack.calls("nats-passwords --check .env")
    assert len(checks) == 1, stack.log
    assert stack.calls("verify-release 0.2.0")[0] < checks[0] < stack.calls(" pull")[0]

    (stack.root / "calls.log").unlink()
    (stack.root / "stack" / ".env").write_text("MOWDECK_VERSION=0.1.0\n", encoding="utf-8")
    result = stack.run("0.2.0", FAKE_NATS_EXIT="1")
    assert result.returncode != 0
    assert "./nats-passwords.sh .env" in result.stderr
    assert not stack.calls(" pull")
    assert not stack.calls("maintenance")
    assert "MOWDECK_VERSION=0.1.0" in (stack.root / "stack" / ".env").read_text("utf-8")


def test_deploy_reloads_the_permissions_of_the_running_nats_before_up(stack: Stack) -> None:
    """The v0.69.4 deploy: `up` does not recreate NATS (its service definition did not change),
    and a running NATS keeps the permissions it read at its start, so the new services were
    refused their new consumer. The script reloads NATS after the checkout, before `up`, on
    every deploy (a rerun after a failed one finds the new files already checked out)."""
    for attempt in (1, 2):
        result = stack.run("0.2.0")
        assert result.returncode == 0, result.stderr
        hup = stack.calls(" kill -s HUP nats")
        logs = stack.calls(" logs --no-color --since ")
        ups = stack.calls(" up -d")
        assert len(hup) == len(ups) == attempt, stack.log
        assert stack.calls("git checkout --detach v0.2.0")[-1] < hup[-1]
        assert stack.calls("maintenance open")[-1] < hup[-1] < logs[-1] < ups[-1]
        assert "-f docker-compose.yml -f docker-compose.release.yml" in stack.log[hup[-1]]
        assert "NATS reloaded its users and permissions" in result.stdout


def test_a_stopped_nats_reads_the_new_files_when_up_starts_it(stack: Stack) -> None:
    result = stack.run("0.2.0", FAKE_NATS_STOPPED="1")
    assert result.returncode == 0, result.stderr
    assert not stack.calls("kill -s HUP")
    assert "NATS is not running" in result.stdout
    assert stack.calls(" up -d")


@pytest.mark.parametrize(
    ("reload", "message"), [("fail", "refused the new configuration"), ("silent", "within 1 s")]
)
def test_a_failed_nats_reload_stops_before_up(stack: Stack, reload: str, message: str) -> None:
    """NATS keeps the old configuration when the new one does not parse; starting the services
    then would only restart-loop them. The window is closed again."""
    result = stack.run("0.2.0", FAKE_NATS_RELOAD=reload, MOWDECK_NATS_RELOAD_SECONDS="1")
    assert result.returncode != 0
    assert message in result.stderr
    assert stack.calls("kill -s HUP nats")
    assert not stack.calls(" up -d")
    assert stack.calls("maintenance close")


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
    assert not stack.env_backups.exists()
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


def _seed(directory: Path, names: list[str], text: str = "old") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(text, encoding="utf-8")


def _dumps(count: int) -> list[str]:
    return [f"mowdeck-before-0.1.{i}-20260101T0000{i:02d}Z.sql.gz" for i in range(count)]


def _envs(count: int) -> list[str]:
    return [f"env-before-0.1.{i}-20260101T0000{i:02d}Z.env" for i in range(count)]


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_rotation_keeps_the_newest_three_by_default_and_the_new_dump(stack: Stack) -> None:
    _seed(stack.backups, _dumps(5))
    _seed(stack.env_backups, _envs(5))
    result = stack.run("0.2.0")
    assert result.returncode == 0, result.stderr
    kept = _names(stack.backups)
    assert kept[:2] == _dumps(5)[3:]  # the two newest old ones; the new one sorts last
    assert len(kept) == 3
    assert "0.2.0" in kept[2]
    assert len(_names(stack.env_backups)) == 3


def test_rotation_count_is_configurable(stack: Stack) -> None:
    _seed(stack.backups, _dumps(4))
    assert stack.run("0.2.0", MOWDECK_BACKUP_KEEP="1").returncode == 0
    assert len(_names(stack.backups)) == 1
    _seed(stack.backups, _dumps(6))
    assert stack.run("0.2.1", MOWDECK_BACKUP_KEEP="5").returncode == 0
    assert len(_names(stack.backups)) == 5


@pytest.mark.parametrize("value", ["0", "-1", "abc", "101", "2.5"])
def test_deploy_rejects_a_bad_keep_count_before_touching_anything(stack: Stack, value: str) -> None:
    _seed(stack.backups, _dumps(2))
    result = stack.run("0.2.0", MOWDECK_BACKUP_KEEP=value)
    assert result.returncode != 0
    assert "MOWDECK_BACKUP_KEEP" in result.stderr
    assert stack.log == []
    assert _names(stack.backups) == _dumps(2)


@pytest.mark.parametrize("failure", ["truncated", "empty", "fail"])
def test_a_failed_or_empty_dump_aborts_and_deletes_nothing(stack: Stack, failure: str) -> None:
    _seed(stack.backups, _dumps(3))
    _seed(stack.env_backups, _envs(3))
    result = stack.run("0.2.0", MOWDECK_BACKUP_KEEP="1", FAKE_DUMP=failure)
    assert result.returncode != 0
    assert _names(stack.backups) == _dumps(3)  # no partial file, no deletion
    assert _names(stack.env_backups) == _envs(3)
    assert not stack.calls("git checkout")
    assert not stack.calls(" up ")


def test_rotation_leaves_foreign_files_and_directories_alone(stack: Stack) -> None:
    _seed(stack.backups, [*_dumps(4), "notes.txt", "mowdeck-before-0.1.0.sql.gz"])
    (stack.backups / "subdir").mkdir()
    assert stack.run("0.2.0", MOWDECK_BACKUP_KEEP="1").returncode == 0
    names = _names(stack.backups)
    assert {"notes.txt", "mowdeck-before-0.1.0.sql.gz", "subdir"} <= set(names)
    assert len([n for n in names if n.startswith("mowdeck-before-0.2.0-")]) == 1
    assert not [n for n in names if n in _dumps(4)]


def test_a_rerun_is_idempotent(stack: Stack) -> None:
    _seed(stack.backups, _dumps(5))
    assert stack.run("0.2.0").returncode == 0
    first = _names(stack.backups)
    assert stack.run("0.2.1").returncode == 0
    second = _names(stack.backups)
    assert len(first) == len(second) == 3
    assert any("0.2.1" in n for n in second)
    assert first[1:] == [n for n in second if "0.2.1" not in n]  # only the oldest went


def test_earlier_env_copies_beside_the_dumps_move_to_the_env_directory(stack: Stack) -> None:
    _seed(stack.backups, _envs(2), text=f"POSTGRES_PASSWORD={SECRET}\n")
    result = stack.run("0.2.0")
    assert result.returncode == 0, result.stderr
    assert not list(stack.backups.glob("*.env"))
    assert len(list(stack.env_backups.glob("env-before-*.env"))) == 3
    assert "moved 2 earlier .env copies" in result.stderr
    assert SECRET not in result.stdout + result.stderr


def test_the_env_directory_must_not_sit_inside_the_dump_directory(stack: Stack) -> None:
    for inside in (stack.backups / "env", stack.backups):
        result = stack.run("0.2.0", MOWDECK_ENV_BACKUP_DIR=inside.as_posix())
        assert result.returncode != 0
        assert "MOWDECK_ENV_BACKUP_DIR" in result.stderr
    result = stack.run("0.2.0", MOWDECK_BACKUP_DIR=(stack.env_backups / "dumps").as_posix())
    assert result.returncode != 0
    assert stack.log == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_dumps_env_copies_and_directories_are_private(stack: Stack) -> None:
    stack.backups.mkdir()
    stack.backups.chmod(0o755)  # an older, too open directory is tightened
    assert stack.run("0.2.0").returncode == 0
    for directory in (stack.backups, stack.env_backups):
        assert directory.stat().st_mode & 0o777 == 0o700
        files = list(directory.iterdir())
        assert files
        assert {path.stat().st_mode & 0o777 for path in files} == {0o600}


@pytest.mark.parametrize("name", ["deploy.sh", "backup-lib.sh"])
def test_deploy_script_is_lf_executable_and_shellcheck_clean(repo_root: Path, name: str) -> None:
    path = repo_root / "infra" / "compose" / name
    assert b"\r" not in path.read_bytes()
    if shutil.which("git") and (repo_root / ".git").exists():
        result = subprocess.run(  # noqa: S603
            ["git", "ls-files", "--stage", f"infra/compose/{name}"],  # noqa: S607
            capture_output=True,
            text=True,
            cwd=repo_root,
            check=False,
        )
        assert result.stdout.startswith("100755"), "git must keep the executable bit"
    if shutil.which("shellcheck") is not None:
        lint = subprocess.run(  # noqa: S603
            ["shellcheck", "-x", path.name],  # noqa: S607
            cwd=path.parent,  # the sourced backup-lib.sh is found relative to the script
            capture_output=True,
            text=True,
            check=False,
        )
        assert lint.returncode == 0, lint.stdout
