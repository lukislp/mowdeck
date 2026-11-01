"""demo/reset.sh (ADR 0068, docs/ops/demo.md): its safety checks and the optional host override.

Most tests run the script against a stand-in ``docker`` that records its arguments and answers
``compose config`` like the real one would, so no container is touched. The allow-list of the
override (demo/override_guard.py) is tested on its own and, when Docker Compose is installed,
end to end with the real ``docker compose config`` of the demo stack (``--dry-run`` only ever
reads the configuration).
"""

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

BASH = shutil.which("bash")
# Windows' System32\bash.exe starts WSL, which sees other paths: use Git's bash there.
needs_bash = pytest.mark.skipif(
    BASH is None or "system32" in BASH.lower(), reason="no POSIX bash on the PATH"
)

SECRETS = (
    "POSTGRES_PASSWORD",
    "SERVICE_DB_PASSWORD",
    "OPENFGA_AUTHN_PRESHARED_KEYS",
    "IDENTITY_KEY_ENCRYPTION_KEY",
    "DEMO_SEED_TOKEN",
)
SECRET_VALUE = "0123456789abcdef0123456789abcdef"  # noqa: S105 - a test value

# The stand-in docker: every call is appended to $FAKE_DOCKER_LOG. With the override among the
# files, FAKE_ADD_SERVICE adds a service and FAKE_DEMO_OFF turns the demo mode of fleet off.
FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
override=0
case " $* " in *demo.override.yml*) override=1 ;; esac
case " $* " in
  *" config --format json "*)
    demo='"true"'
    extra=''
    if [ "$override" = 1 ] && [ -n "${FAKE_DEMO_OFF:-}" ]; then demo='"false"'; fi
    if [ "$override" = 1 ] && [ -n "${FAKE_ADD_SERVICE:-}" ]; then extra=', "billing": {}'; fi
    fleet="{\"environment\": {\"MOWDECK_DEMO_MODE\": $demo, \"SECRET\": \"$FAKE_SECRET\"}}"
    services="{\"fleet\": $fleet, \"traefik\": {}$extra}"
    networks='{"mowdeck": {"name": "mowdeck-demo"}}'
    printf '{"name": "mowdeck-demo", "services": %s, "networks": %s}\n' "$services" "$networks"
    ;;
esac
exit 0
"""


def _guard_module(repo_root: Path) -> ModuleType:
    path = repo_root / "infra" / "compose" / "demo" / "override_guard.py"
    spec = importlib.util.spec_from_file_location("override_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bin(tmp_path: Path, *, fake_docker: bool) -> Path:
    """A bin directory with python3 (this interpreter) and, if asked, the stand-in docker."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    scripts = {"python3": f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n'}
    if fake_docker:
        scripts["docker"] = FAKE_DOCKER
    for name, text in scripts.items():
        path = bin_dir / name
        path.write_text(text, "utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _nats_passwords(compose: Path) -> list[str]:
    """The NATS_PASSWORD_* variables of nats/auth.conf (ADR 0074)."""
    text = (compose / "nats" / "auth.conf").read_text("utf-8")
    return sorted(set(re.findall(r"\$(NATS_PASSWORD_[A-Z0-9_]+)", text)))


def _demo_env(compose: Path, *extra: str) -> None:
    lines = [
        "COMPOSE_PROJECT_NAME=mowdeck-demo",
        "MOWDECK_NETWORK=mowdeck-demo",
        "MOWDECK_TRAEFIK_PROJECT=mowdeck-demo",
        "COMPOSE_PROFILES=",
        *(f"{name}={SECRET_VALUE}" for name in SECRETS),
        # A NATS password starts with a letter (the server parses it as a config value).
        *(f"{name}=n{SECRET_VALUE}" for name in _nats_passwords(compose)),
        *extra,
    ]
    (compose / "demo" / "demo.env").write_text("\n".join(lines) + "\n", "utf-8", newline="\n")


@pytest.fixture
def stack(repo_root: Path, tmp_path: Path) -> Path:
    """A copy of reset.sh, the guard and the NATS password check with a filled demo.env and the
    stand-in docker."""
    compose = tmp_path / "compose"
    (compose / "demo").mkdir(parents=True)
    (compose / "nats").mkdir()
    for name in ("reset.sh", "override_guard.py"):
        shutil.copy(repo_root / "infra" / "compose" / "demo" / name, compose / "demo")
    shutil.copy(repo_root / "infra" / "compose" / "nats-passwords.sh", compose)
    shutil.copy(repo_root / "infra" / "compose" / "nats" / "auth.conf", compose / "nats")
    _demo_env(compose)
    _bin(tmp_path, fake_docker=True)
    return compose


def _run(stack: Path, *args: str, **env: str) -> tuple[subprocess.CompletedProcess[str], str]:
    log = stack.parent / "docker.log"
    log.write_text("", "utf-8")
    bin_dir = (stack.parent / "bin").as_posix()
    environ = {k: v for k, v in os.environ.items() if k != "COMPOSE_PROFILES"}
    environ |= {"FAKE_DOCKER_LOG": log.as_posix(), "FAKE_SECRET": SECRET_VALUE, **env}
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


@needs_bash
def test_without_override_the_two_compose_files(stack: Path) -> None:
    result, calls = _run(stack, "--no-build")
    assert result.returncode == 0, result.stderr
    assert "demo.override.yml" not in calls
    assert "down --volumes --remove-orphans" in calls
    assert "up -d --no-build --wait" in calls
    assert "-f" in calls and "docker-compose.demo.yml" in calls


@needs_bash
def test_override_is_added_as_the_last_file(stack: Path) -> None:
    (stack / "demo" / "demo.override.yml").write_text("services: {}\n", "utf-8")
    result, calls = _run(stack, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "using" in result.stdout and "demo.override.yml" in result.stdout
    up = next(line for line in result.stdout.splitlines() if " up -d " in line)
    assert up.index("docker-compose.demo.yml") < up.index("demo.override.yml")
    # A dry run only reads the configuration.
    assert all(" config" in line for line in calls.splitlines())


@needs_bash
@pytest.mark.parametrize(
    ("variable", "path"),
    [
        ("FAKE_ADD_SERVICE", "services.billing"),
        ("FAKE_DEMO_OFF", "services.fleet.environment.MOWDECK_DEMO_MODE"),
    ],
)
def test_override_cannot_weaken_the_demo(stack: Path, variable: str, path: str) -> None:
    (stack / "demo" / "demo.override.yml").write_text("services: {}\n", "utf-8")
    result, calls = _run(stack, **{variable: "1"})
    assert result.returncode == 1
    assert f"refused {path}:" in result.stderr
    assert "may only add networks, resource limits and logging" in result.stderr
    assert SECRET_VALUE not in result.stdout + result.stderr
    assert " down " not in calls and " up " not in calls


@needs_bash
@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("COMPOSE_PROJECT_NAME", "mowdeck", "must not be mowdeck"),
        ("COMPOSE_PROFILES", "billing", "COMPOSE_PROFILES must stay empty"),
        ("DEMO_SEED_TOKEN", "change-me", "set DEMO_SEED_TOKEN"),
        ("NATS_PASSWORD_FLEET", "change-me", "set the NATS passwords"),
        ("NATS_PASSWORD_STREAMS", "", "set the NATS passwords"),
        ("NATS_PASSWORD_VISION", "1digit-first-password-0123456789", "set the NATS passwords"),
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


# --- the allow-list on its own ----------------------------------------------------------------

BASE: dict[str, Any] = {
    "name": "mowdeck-demo",
    "services": {
        "traefik": {
            "image": "traefik:v3.7.14",
            "command": ["--api.dashboard=false"],
            "networks": {"mowdeck": None},
            "ports": [{"target": 80, "published": "8580", "host_ip": "127.0.0.1"}],
        },
        "identity": {
            "environment": {"MOWDECK_DEMO_MODE": "true", "MOWDECK_DEMO_SEED_TOKEN": SECRET_VALUE},
            "labels": {"traefik.http.routers.identity-demo.middlewares": "identity-ratelimit"},
            "networks": {"mowdeck": None},
        },
    },
    "networks": {"mowdeck": {"name": "mowdeck-demo"}},
}


DROP = "<drop>"
"""A value in a ``_with`` change that removes the key."""


def _with(change: dict[str, Any]) -> dict[str, Any]:
    def merge(left: Any, right: Any) -> Any:
        if isinstance(left, dict) and isinstance(right, dict):
            return {
                k: merge(left.get(k), right[k]) if k in right else left[k]
                for k in left | right
                if right.get(k) != DROP
            }
        return right

    result: dict[str, Any] = merge(json.loads(json.dumps(BASE)), change)
    return result


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"services": {"traefik": {"mem_limit": "268435456", "cpus": 0.5, "pids_limit": 200}}},
        {"services": {"identity": {"deploy": {"resources": {"limits": {"memory": "512m"}}}}}},
        # What `docker compose config` renders for the line above: `placement: {}` comes along.
        {
            "services": {
                "identity": {
                    "deploy": {"resources": {"limits": {"memory": "536870912"}}, "placement": {}}
                }
            }
        },
        {"services": {"identity": {"logging": {"driver": "json-file"}}}},
        {
            "services": {"traefik": {"networks": {"edge": {"aliases": ["mowdeck-demo-gateway"]}}}},
            "networks": {"edge": {"name": "mowdeck-demo-edge", "external": True}},
        },
        # `mowdeck: {}` in the override where the base renders `mowdeck: null`: unchanged.
        {"services": {"traefik": {"networks": {"mowdeck": {}}}}},
    ],
)
def test_guard_accepts_networks_limits_and_logging(repo_root: Path, change: dict[str, Any]) -> None:
    guard = _guard_module(repo_root)
    assert guard.check(BASE, _with(change)) == []


@pytest.mark.parametrize(
    ("change", "path"),
    [
        ({"services": {"billing": {"image": "x"}}}, "services.billing"),
        ({"services": {"traefik": {"image": "alpine:3"}}}, "services.traefik.image"),
        (
            {"services": {"traefik": {"command": ["--api.insecure=true"]}}},
            "services.traefik.command",
        ),
        ({"services": {"traefik": {"ports": []}}}, "services.traefik.ports"),
        ({"services": {"traefik": {"network_mode": "host"}}}, "services.traefik.network_mode"),
        (
            {"services": {"identity": {"environment": {"MOWDECK_CLIENT_IP_DEPTH": "3"}}}},
            "services.identity.environment.MOWDECK_CLIENT_IP_DEPTH",
        ),
        (
            {"services": {"identity": {"labels": {"traefik.http.routers.zz.rule": "Path(`/`)"}}}},
            "services.identity.labels.traefik.http.routers.zz.rule",
        ),
        (
            {"services": {"identity": {"deploy": {"replicas": 3}}}},
            "services.identity.deploy.replicas",
        ),
        (
            {"services": {"identity": {"deploy": {"placement": {"constraints": ["x"]}}}}},
            "services.identity.deploy.placement.constraints",
        ),
        ({"services": {"identity": {"volumes": []}}}, "services.identity.volumes"),
        # Networks: only the gateway joins networks, and only networks outside the demo.
        (
            {
                "services": {"identity": {"networks": {"edge": {}}}},
                "networks": {"edge": {"name": "edge", "external": True}},
            },
            "services.identity.networks",
        ),
        (
            {"services": {"identity": {"networks": {"mowdeck": {"aliases": ["traefik"]}}}}},
            "services.identity.networks",
        ),
        (
            {"services": {"traefik": {"networks": {"mowdeck": {"aliases": ["identity"]}}}}},
            "services.traefik.networks.mowdeck",
        ),
        (
            {"services": {"traefik": {"networks": {"mowdeck": DROP}}}},
            "services.traefik.networks.mowdeck",
        ),
        (
            {
                "services": {"traefik": {"networks": {"edge": {"aliases": ["gw", "identity"]}}}},
                "networks": {"edge": {"name": "edge", "external": True}},
            },
            "services.traefik.networks.edge.aliases",
        ),
        ({"networks": {"mowdeck": {"internal": False}}}, "networks.mowdeck.internal"),
        ({"networks": {"open": {"driver": "bridge"}}}, "networks.open"),
        ({"volumes": {"data": {}}}, "volumes"),
        ({"name": "other"}, "name"),
    ],
)
def test_guard_refuses_everything_else(repo_root: Path, change: dict[str, Any], path: str) -> None:
    guard = _guard_module(repo_root)
    refused = guard.check(BASE, _with(change))
    assert any(line.startswith(f"{path}:") or line.startswith(f"{path}.") for line in refused), (
        refused
    )
    assert SECRET_VALUE not in "".join(refused)


def test_guard_never_prints_values(repo_root: Path) -> None:
    """The refusal names paths only: a changed secret does not show up."""
    secret = "f" * 64
    change = {"services": {"identity": {"environment": {"MOWDECK_DEMO_SEED_TOKEN": secret}}}}
    payload = json.dumps({"base": BASE, "override": _with(change)})
    script = repo_root / "infra" / "compose" / "demo" / "override_guard.py"
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(script)], input=payload, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert "services.identity.environment.MOWDECK_DEMO_SEED_TOKEN" in result.stderr
    assert secret not in result.stdout + result.stderr
    assert SECRET_VALUE not in result.stdout + result.stderr


def test_guard_refuses_unreadable_input(repo_root: Path) -> None:
    script = repo_root / "infra" / "compose" / "demo" / "override_guard.py"
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(script)], input="{", capture_output=True, text=True, check=False
    )
    assert result.returncode == 2


# --- end to end with the real docker compose config ------------------------------------------


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "compose", "version"],  # noqa: S607
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


needs_compose = pytest.mark.skipif(not _compose_available(), reason="no docker compose")

ALLOWED_EDGE = """
services:
  traefik:
    networks:
      mowdeck: {}
      mowdeck-demo-edge:
        aliases: [mowdeck-demo-gateway]
    mem_limit: 256m
    cpus: 0.5
  postgres:
    mem_limit: 512m
networks:
  mowdeck-demo-edge:
    external: true
"""


def _traefik_command(*flags: str) -> str:
    """An override that re-lists Traefik's command: the base providers and entry points (the
    lines the earlier deny-list guarded, unchanged), then ``flags``."""
    project = "`com.docker.compose.project`,`${MOWDECK_TRAEFIK_PROJECT:-mowdeck}`"
    lines = [
        "--providers.docker=true",
        "--providers.docker.exposedbydefault=false",
        "--providers.docker.network=${MOWDECK_NETWORK:-mowdeck}",
        f"--providers.docker.constraints=Label({project})",
        "--entrypoints.web.address=:80",
        "--entrypoints.websecure.address=:443",
        *flags,
    ]
    return "services:\n  traefik:\n    command:\n" + "".join(f"      - {x}\n" for x in lines)


MIDDLEWARES = "http.middlewares=demo-headers@docker,demo-ratelimit@docker"
TRUSTED = "forwardedHeaders.trustedIPs=${GATEWAY_TRUSTED_PROXIES:-}"

# The bypasses of the network audit (all must be refused), and the earlier deny-list cases.
REFUSED = {
    "demo-mode-off": 'services:\n  fleet:\n    environment:\n      MOWDECK_DEMO_MODE: "false"\n',
    "enable-profile-whoami": "services:\n  whoami:\n    profiles: !reset []\n",
    "publish-postgres": (
        'services:\n  postgres:\n    ports: !override\n      - "0.0.0.0:5432:5432"\n'
    ),
    "publish-gateway-everywhere": (
        'services:\n  traefik:\n    ports: !override\n      - "0.0.0.0:8580:80"\n'
    ),
    "publish-nats": (
        "services:\n  nats:\n    ports: !override\n"
        '      - "0.0.0.0:4222:4222"\n      - "0.0.0.0:8222:8222"\n'
    ),
    "traefik-api-insecure": _traefik_command(
        f"--entrypoints.web.{TRUSTED}",
        f"--entrypoints.websecure.{TRUSTED}",
        f"--entrypoints.web.{MIDDLEWARES}",
        f"--entrypoints.websecure.{MIDDLEWARES}",
        "--api.insecure=true",
        "--api.dashboard=true",
        "--ping=true",
    ),
    "trust-every-forwarded-header": _traefik_command(
        "--entrypoints.web.forwardedHeaders.insecure=true",
        f"--entrypoints.web.{MIDDLEWARES}",
        f"--entrypoints.websecure.{MIDDLEWARES}",
        "--api.dashboard=false",
        "--ping=true",
    ),
    "router-without-login-limit": (
        "services:\n  identity:\n    labels:\n"
        "      - traefik.http.routers.zz.rule=Path(`/api/v1/identity/demo/session`)\n"
        "      - traefik.http.routers.zz.entrypoints=web\n"
        "      - traefik.http.routers.zz.priority=1000\n"
        "      - traefik.http.routers.zz.service=identity\n"
    ),
    "client-ip-depth-env": (
        "services:\n  identity:\n    environment:\n"
        '      MOWDECK_CLIENT_IP_DEPTH: "3"\n      MOWDECK_IDENTITY_TRUST_FORWARDED_FOR: "true"\n'
    ),
    "image-swap": (
        'services:\n  fleet:\n    image: alpine:3\n    command: ["sh", "-c", "sleep infinity"]\n'
    ),
    "seed-history": 'services:\n  demo-seed:\n    environment:\n      DEMO_HISTORY_DAYS: "31"\n',
    "replicas": "services:\n  realtime:\n    deploy:\n      replicas: 3\n",
    "own-network": "services:\n  nats:\n    networks: [open]\nnetworks:\n  open: {}\n",
    "nats-without-authentication": (
        'services:\n  nats:\n    command: ["-js", "-m", "8222", "-sd", "/data"]\n'
    ),
    "nats-on-edge": (
        "services:\n  nats:\n    networks:\n      mowdeck: {}\n      mowdeck-demo-edge: {}\n"
        "networks:\n  mowdeck-demo-edge:\n    external: true\n"
    ),
    "alias-on-the-demo-network": (
        "services:\n  traefik:\n    networks:\n      mowdeck:\n        aliases: [nats]\n"
    ),
    "alias-shadows-a-service": (
        "services:\n  traefik:\n    networks:\n      mowdeck: {}\n"
        "      mowdeck-demo-edge:\n        aliases: [identity]\n"
        "networks:\n  mowdeck-demo-edge:\n    external: true\n"
    ),
}


@pytest.fixture
def real_stack(repo_root: Path, tmp_path: Path) -> Path:
    """A copy of infra/compose (without host files) with a filled demo.env; real docker."""
    source = repo_root / "infra" / "compose"
    compose = tmp_path / "compose"
    shutil.copytree(
        source,
        compose,
        ignore=shutil.ignore_patterns("demo.env", "demo.override.yml", "secrets", ".env"),
    )
    _demo_env(compose, "COMPOSE_PROJECT_NAME=override-guard-test")
    for name in ("MOWDECK_NETWORK", "MOWDECK_TRAEFIK_PROJECT"):
        _env(compose, name, "override-guard-test")
    _bin(tmp_path, fake_docker=False)
    return compose


ALLOWED = {
    "edge-network": ALLOWED_EDGE,
    # Compose renders `deploy.placement: {}` along with these.
    "deploy-resources": (
        "services:\n  identity:\n    deploy:\n      resources:\n"
        "        limits:\n          memory: 256m\n          cpus: '0.5'\n"
    ),
}


@needs_bash
@needs_compose
@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_real_compose_accepts_the_documented_overrides(real_stack: Path, name: str) -> None:
    (real_stack / "demo" / "demo.override.yml").write_text(ALLOWED[name], "utf-8")
    result, _ = _run(real_stack, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "using" in result.stdout
    assert SECRET_VALUE not in result.stdout + result.stderr


@needs_bash
@needs_compose
@pytest.mark.parametrize("name", sorted(REFUSED))
def test_real_compose_refuses_the_audit_bypasses(real_stack: Path, name: str) -> None:
    (real_stack / "demo" / "demo.override.yml").write_text(REFUSED[name], "utf-8")
    result, _ = _run(real_stack, "--dry-run")
    assert result.returncode == 1, (name, result.stdout, result.stderr)
    assert "demo-reset:" in result.stderr
    assert " up -d " not in result.stdout
    assert SECRET_VALUE not in result.stdout + result.stderr
