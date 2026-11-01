"""NATS authentication and least-privilege permissions (ADR 0074).

* The generated server configuration (infra/compose/nats/auth.conf and the chart's copy) is the
  rendering of the subject map in tools/repo-checks/scripts/nats_auth.py.
* The map and the code agree: everything a service's code does on the bus is allowed, and
  everything allowed is done by the code.
* Compose gives every service its own user and only its own password; the NATS server gets
  them all; the env templates list every password; the password script works and never prints
  a value.
"""

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml


def _module(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "repo-checks" / "scripts" / "nats_auth.py"
    spec = importlib.util.spec_from_file_location("nats_auth", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen(repo_root: Path) -> ModuleType:
    return _module(repo_root)


def _compose(repo_root: Path) -> dict[str, Any]:
    text = (repo_root / "infra" / "compose" / "docker-compose.yml").read_text("utf-8")
    services: dict[str, Any] = yaml.safe_load(text)["services"]
    return services


def _password_names(repo_root: Path) -> set[str]:
    text = (repo_root / "infra" / "compose" / "nats" / "auth.conf").read_text("utf-8")
    return set(re.findall(r"\$(NATS_PASSWORD_[A-Z0-9_]+)", text))


def test_the_committed_configuration_is_fresh(gen: ModuleType, repo_root: Path) -> None:
    assert gen.stale(repo_root) == [], "run `uv run poe nats-auth`"
    assert gen.main(["--check", "--root", str(repo_root)]) == 0


def test_the_map_is_valid(gen: ModuleType) -> None:
    assert gen.check_users(gen.USERS) == []
    bad = {"x": gen.Access(serve=("event.a.b",), consume=("fleet.mowers.list",))}
    problems = gen.check_users(bad)
    assert any("serves event.a.b" in p for p in problems)
    assert any("consumes fleet.mowers.list" in p for p in problems)
    with pytest.raises(ValueError, match="no stream stores"):
        gen.render(bad)


def test_the_map_and_the_code_agree(gen: ModuleType, repo_root: Path) -> None:
    """Every bus use of a service's code is allowed, and every allowed subject is used."""
    used = gen.scan(repo_root)
    missing, unused = gen.drift(gen.USERS, used)
    assert missing == [], "the code uses subjects nats_auth.USERS does not allow"
    assert unused == [], "nats_auth.USERS allows subjects no code uses (least privilege)"
    # The scan sees the cases that matter: requests, served subjects, consumers and events.
    assert ("publish", "fleet.mowers.credentials") in used["device-gateway"]
    assert ("serve", "fleet.mowers.credentials") in used["fleet"]
    assert ("consume", "command.mower.*") in used["device-gateway"]
    assert ("publish", "event.mower.registered") in used["fleet"]
    assert ("publish", "event.identity.session.new_device") in used["identity"]
    assert ("consume", "event.identity.grants.changed") in used["billing"]
    assert ("publish", "entitlements.check") in used["planner"]


def test_drift_reports_both_directions(gen: ModuleType) -> None:
    users = {"svc": gen.Access(publish=("a.b",), consume=("event.x.>",))}
    used = {"svc": {("publish", "a.c"), ("consume", "event.x.y")}, "other": {("publish", "a.b")}}
    missing, unused = gen.drift(users, used)
    assert missing == ["other: uses the bus but has no NATS user", "svc: publish a.c"]
    assert unused == ["svc: publish a.b"]


@pytest.mark.parametrize(
    ("pattern", "subject", "expected"),
    [
        ("event.mower.>", "event.mower.registered", True),
        ("event.mower.>", "event.mower.>", True),
        ("event.mower.*", "event.mower.>", False),
        ("event.mower.registered", "event.mower.*", False),
        ("gateway.mower.*.photo", "gateway.mower.*.photo", True),
        ("gateway.mower.*.photo", "gateway.mower.*.tracking", False),
        ("event.>", "event", False),
    ],
)
def test_covers(gen: ModuleType, pattern: str, subject: str, expected: bool) -> None:
    assert gen.covers(pattern, subject) is expected


def _overlap(a: str, b: str) -> bool:
    left, right = a.split("."), b.split(".")
    for index in range(max(len(left), len(right))):
        x = left[index] if index < len(left) else None
        y = right[index] if index < len(right) else None
        if ">" in (x, y):
            return x is not None and y is not None
        if x is None or y is None:
            return False
        if "*" not in (x, y) and x != y:
            return False
    return True


def test_each_event_has_one_publisher(gen: ModuleType) -> None:
    """A domain event comes from exactly one service: no two users may publish one subject."""
    owners = [
        (user, subject)
        for user, access in gen.USERS.items()
        for subject in access.publish
        if subject.startswith("event.")
    ]
    clashes = [
        (a, b)
        for i, a in enumerate(owners)
        for b in owners[i + 1 :]
        if a[0] != b[0] and _overlap(a[1], b[1])
    ]
    assert clashes == []


def test_the_permissions_are_least_privilege(gen: ModuleType) -> None:
    config = gen.render()
    blocks = {
        user: config.split(f'user: "{user}"', 1)[1].split("\n    }\n", 1)[0] for user in gen.USERS
    }
    for user, block in blocks.items():
        # Only the own inbox, never another user's or the whole _INBOX.
        assert re.findall(r'"(_INBOX[^"]*)"', block) == [f"_INBOX.{user}.>"], user
        # The legacy consumer API (any filter) is never allowed.
        assert "CONSUMER.DURABLE.CREATE" not in block
        assert not re.search(r'"\$JS\.API\.CONSUMER\.CREATE\.[A-Z]+"', block)
        assert "STREAM.DELETE" not in block and "STREAM.PURGE" not in block
        assert '"$JS.API.>"' not in block and '">"' not in block
        manages = "STREAM.CREATE" in block or "STREAM.UPDATE" in block
        assert manages is (user == gen.STREAMS_USER), user
        assert ("allow_responses: true" in block) is bool(gen.USERS[user].serve), user
    assert '"$JS.API.CONSUMER.CREATE.COMMANDS.*.command.mower.*"' in blocks["device-gateway"]
    assert "fleet.mowers.credentials" not in blocks["vision"]


def test_compose_gives_every_service_its_own_user(gen: ModuleType, repo_root: Path) -> None:
    services = _compose(repo_root)
    users: dict[str, str] = {}
    for name, service in services.items():
        env = service.get("environment") or {}
        if "MOWDECK_NATS_URL" not in env:
            continue
        user = env.get("MOWDECK_NATS_USER")
        assert user, f"{name} connects to NATS without MOWDECK_NATS_USER"
        assert user == (gen.STREAMS_USER if name == "nats-streams" else name)
        variable = gen.password_variable(user)
        assert str(env.get("MOWDECK_NATS_PASSWORD", "")).startswith(f"${{{variable}:?"), name
        # Only its own password: no service sees another one.
        others = [v for v in map(str, env.values()) if "NATS_PASSWORD_" in v]
        assert others == [env["MOWDECK_NATS_PASSWORD"]], name
        if name != "nats-streams":
            depends = service["depends_on"]
            assert depends["nats-streams"]["condition"] == "service_completed_successfully"
        users[name] = user
    assert sorted(users.values()) == sorted(gen.USERS)

    nats = services["nats"]
    assert nats["command"][:2] == ["-c", "/etc/nats/mowdeck/auth.conf"]
    assert "./nats:/etc/nats/mowdeck:ro" in nats["volumes"]
    assert set(nats["environment"]) == _password_names(repo_root)
    assert _password_names(repo_root) == {gen.password_variable(u) for u in gen.USERS}


def test_the_env_templates_list_every_password(gen: ModuleType, repo_root: Path) -> None:
    compose = repo_root / "infra" / "compose"
    pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{23,127}$")
    for path in (compose / ".env.example", compose / "demo" / "demo.env.example"):
        values = dict(
            line.split("=", 1)
            for line in path.read_text("utf-8").splitlines()
            if line.startswith("NATS_PASSWORD_")
        )
        assert set(values) == _password_names(repo_root), path
        for value in values.values():
            # Development defaults that NATS accepts but deploy.sh and reset.sh refuse.
            assert value.startswith("change-me"), path
            if path.name == ".env.example":
                assert pattern.fullmatch(value), value


def _find_bash() -> str | None:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if os.name == "nt" and git_bash.exists():
        return str(git_bash)
    found = shutil.which("bash")
    return None if found is None or "system32" in found.lower() else found


BASH = _find_bash()


@pytest.mark.skipif(BASH is None, reason="bash not installed")
@pytest.mark.timeout(900)  # five bash runs; Git Bash on Windows starts slowly
def test_the_password_script(repo_root: Path, tmp_path: Path) -> None:
    compose = tmp_path / "compose"
    (compose / "nats").mkdir(parents=True)
    shutil.copy(repo_root / "infra" / "compose" / "nats-passwords.sh", compose)
    shutil.copy(repo_root / "infra" / "compose" / "nats" / "auth.conf", compose / "nats")
    env = compose / ".env"
    kept = "keepThisGoodPasswordOf32Chars0123"
    env.write_text(
        f"POSTGRES_PASSWORD=x\nNATS_PASSWORD_FLEET=change-me-nats-fleet\n"
        f"NATS_PASSWORD_IDENTITY={kept}\nNATS_PASSWORD_VISION=9starts-with-a-digit-0123456789",
        encoding="utf-8",
        newline="\n",
    )

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        assert BASH is not None
        return subprocess.run(  # noqa: S603
            [BASH, (compose / "nats-passwords.sh").as_posix(), *args, env.as_posix()],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )

    check = run("--check")
    assert check.returncode == 1
    assert "NATS_PASSWORD_FLEET" in check.stderr and "NATS_PASSWORD_VISION" in check.stderr
    assert "NATS_PASSWORD_IDENTITY" not in check.stderr

    fill = run()
    assert fill.returncode == 0, fill.stderr
    text = env.read_text("utf-8")
    values = dict(
        line.split("=", 1) for line in text.splitlines() if line.startswith("NATS_PASSWORD_")
    )
    assert set(values) == _password_names(repo_root)
    assert values["NATS_PASSWORD_IDENTITY"] == kept
    generated = [v for k, v in values.items() if k != "NATS_PASSWORD_IDENTITY"]
    assert all(re.fullmatch(r"n[0-9a-f]{48}", v) for v in generated)
    assert len(set(generated)) == len(generated)
    assert text.startswith("POSTGRES_PASSWORD=x\n")
    assert text.count("NATS_PASSWORD_FLEET=") == 1  # replaced in place, not appended
    for value in values.values():
        assert value not in fill.stdout + fill.stderr

    assert run("--check").returncode == 0
    again = run()
    assert again.returncode == 0
    assert "nothing changed" in again.stdout
    assert env.read_text("utf-8") == text
