"""demo/reset.sh (ADR 0068, docs/ops/demo.md): its safety checks and the optional host override.

The script runs against a stand-in ``docker`` that records its arguments and answers
``compose config`` like the real one would, so no container is touched.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

BASH = shutil.which("bash")
# Windows' System32ash.exe starts WSL, which sees other paths: use Git's bash there.
pytestmark = pytest.mark.skipif(
    BASH is None or "system32" in BASH.lower(), reason="no POSIX bash on the PATH"
)

SECRETS = (
    "POSTGRES_PASSWORD",
    "SERVICE_DB_PASSWORD",
    "OPENFGA_AUTHN_PRESHARED_KEYS",
    "IDENTITY_KEY_ENCRYPTION_KEY",
    "DEMO_SEED_TOKEN",
)

# The stand-in docker: every call is appended to $FAKE_DOCKER_LOG. With the override among the
# files, FAKE_ADD_SERVICE adds a service and FAKE_DEMO_OFF turns the demo mode of fleet off.
FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
override=0
case " $* " in *demo.override.yml*) override=1 ;; esac
case " $* " in
  *" config --services "*)
    printf 'identity\nfleet\ntraefik\n'
    if [ "$override" = 1 ] && [ -n "${FAKE_ADD_SERVICE:-}" ]; then echo billing; fi
    ;;
  *" config "*)
    echo '      MOWDECK_DEMO_MODE: "true"'
    if [ "$override" = 1 ] && [ -n "${FAKE_DEMO_OFF:-}" ]; then
      echo '      MOWDECK_DEMO_MODE: "false"'
    else
      echo '      MOWDECK_DEMO_MODE: "true"'
    fi
    echo '      - --entrypoints.web.http.middlewares=demo-headers@docker,demo-ratelimit@docker'
    ;;
esac
exit 0
"""


@pytest.fixture
def stack(repo_root: Path, tmp_path: Path) -> Path:
    """A copy of the compose directory with a filled demo.env and the stand-in docker."""
    compose = tmp_path / "compose"
    (compose / "demo").mkdir(parents=True)
    shutil.copy(repo_root / "infra" / "compose" / "demo" / "reset.sh", compose / "demo")
    lines = [
        "COMPOSE_PROJECT_NAME=mowdeck-demo",
        "MOWDECK_NETWORK=mowdeck-demo",
        "MOWDECK_TRAEFIK_PROJECT=mowdeck-demo",
        "COMPOSE_PROFILES=",
        *(f"{name}=0123456789abcdef" for name in SECRETS),
    ]
    (compose / "demo" / "demo.env").write_text("\n".join(lines) + "\n", "utf-8", newline="\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, "utf-8", newline="\n")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return compose


def _run(stack: Path, *args: str, **env: str) -> tuple[subprocess.CompletedProcess[str], str]:
    log = stack.parent / "docker.log"
    log.write_text("", "utf-8")
    bin_dir = (stack.parent / "bin").as_posix()
    environ = {k: v for k, v in os.environ.items() if k != "COMPOSE_PROFILES"}
    environ |= {"FAKE_DOCKER_LOG": log.as_posix(), **env}
    script = (stack / "demo" / "reset.sh").as_posix()
    result = subprocess.run(  # noqa: S603
        [str(BASH), "-c", f'PATH="{bin_dir}:$PATH" exec bash "$0" "$@"', script, *args],
        capture_output=True,
        text=True,
        check=False,
        env=environ,
    )
    return result, log.read_text("utf-8")


def _env(stack: Path, name: str, value: str) -> None:
    path = stack / "demo" / "demo.env"
    path.write_text(path.read_text("utf-8") + f"{name}={value}\n", "utf-8", newline="\n")


def test_without_override_the_two_compose_files(stack: Path) -> None:
    result, calls = _run(stack, "--no-build")
    assert result.returncode == 0, result.stderr
    assert "demo.override.yml" not in calls
    assert "down --volumes --remove-orphans" in calls
    assert "up -d --no-build --wait" in calls
    assert "-f" in calls and "docker-compose.demo.yml" in calls


def test_override_is_added_as_the_last_file(stack: Path) -> None:
    (stack / "demo" / "demo.override.yml").write_text("services: {}\n", "utf-8")
    result, calls = _run(stack, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "using" in result.stdout and "demo.override.yml" in result.stdout
    up = next(line for line in result.stdout.splitlines() if " up -d " in line)
    assert up.index("docker-compose.demo.yml") < up.index("demo.override.yml")
    # A dry run only reads the configuration.
    assert all(" config" in line for line in calls.splitlines())


@pytest.mark.parametrize(
    ("variable", "message"),
    [("FAKE_ADD_SERVICE", "must not add services"), ("FAKE_DEMO_OFF", "must not change")],
)
def test_override_cannot_weaken_the_demo(stack: Path, variable: str, message: str) -> None:
    (stack / "demo" / "demo.override.yml").write_text("services: {}\n", "utf-8")
    result, calls = _run(stack, **{variable: "1"})
    assert result.returncode == 1
    assert message in result.stderr
    assert " down " not in calls and " up " not in calls


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("COMPOSE_PROJECT_NAME", "mowdeck", "must not be mowdeck"),
        ("COMPOSE_PROFILES", "billing", "COMPOSE_PROFILES must stay empty"),
        ("DEMO_SEED_TOKEN", "change-me", "set DEMO_SEED_TOKEN"),
    ],
)
def test_the_safety_checks_hold_with_an_override(
    stack: Path, name: str, value: str, message: str
) -> None:
    (stack / "demo" / "demo.override.yml").write_text("services: {}\n", "utf-8")
    _env(stack, name, value)
    result, calls = _run(stack)
    assert result.returncode == 1
    assert message in result.stderr
    assert calls == ""


def test_the_override_is_git_ignored(repo_root: Path) -> None:
    lines = (repo_root / ".gitignore").read_text("utf-8").splitlines()
    assert "infra/compose/demo/demo.override.yml" in lines
