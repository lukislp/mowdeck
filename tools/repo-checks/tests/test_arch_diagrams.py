"""The generated architecture page matches the code, and the generator reads a small fixture."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

_SUBJECTS = """
from dataclasses import dataclass

COMMAND_PREFIX = "command.mower"
EVENT_PREFIX = "event"
ALL_COMMANDS = f"{COMMAND_PREFIX}.*"
PING = "ping.request"


def command_subject(mower_id):
    return f"{COMMAND_PREFIX}.{mower_id}"


def event_wildcard(domain=None):
    if domain is None:
        return f"{EVENT_PREFIX}.>"
    return f"{EVENT_PREFIX}.{domain}.>"


@dataclass
class StreamSpec:
    name: str
    subjects: tuple


COMMANDS = StreamSpec(name="COMMANDS", subjects=("command.>",))
EVENTS = StreamSpec(name="EVENTS", subjects=("event.>",))
"""

_COMPOSE = """
services:
  traefik:
    image: traefik:v3
  nats:
    image: nats:2
  postgres:
    image: postgres:17
  alpha-migrate:
    image: ${MOWDECK_IMAGE_REGISTRY:-mowdeck}/alpha:dev
    restart: "no"
    environment:
      MOWDECK_DATABASE_URL: postgresql://alpha:pw@postgres:5432/alpha
  alpha:
    image: ${MOWDECK_IMAGE_REGISTRY:-mowdeck}/alpha:dev
    environment:
      MOWDECK_NATS_URL: nats://nats:4222
      MOWDECK_DATABASE_URL: postgresql://alpha:pw@postgres:5432/alpha
      MOWDECK_PEER_URL: http://beta:8000
      MOWDECK_STRIPE_SECRET_KEY: x
    labels:
      - traefik.http.routers.alpha.rule=PathPrefix(`/api/v1/alpha`)
      - traefik.http.routers.alpha-hook.rule=Path(`/api/v1/alpha/hook`)
  beta:
    image: ${MOWDECK_IMAGE_REGISTRY:-mowdeck}/beta:dev
    profiles: [extra]
    environment:
      MOWDECK_NATS_URL: nats://nats:4222
    labels:
      traefik.http.routers.beta.rule: PathPrefix(`/api/v1/beta`)
"""

_ALPHA = """
from subjects import ALL_COMMANDS, command_subject, event_wildcard


async def start(bus, mower_id):
    await bus.publish(command_subject(mower_id), object())
    await bus.subscribe(event_wildcard("beta"), print)
    await bus.request("ping.request", object())
    await bus.publish(variable_subject, object())
"""

_BETA = """
from subjects import ALL_COMMANDS, PING


async def start(bus):
    await bus.subscribe(ALL_COMMANDS, print)
    await bus.subscribe(PING, print)
    await events.publish("beta.done", None, None)
"""


def _generator(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "repo-checks" / "scripts" / "arch_diagrams.py"
    spec = importlib.util.spec_from_file_location("arch_diagrams", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    _write(tmp_path / "infra/compose/docker-compose.yml", _COMPOSE)
    _write(tmp_path / "libs/mowdeck-contracts/src/mowdeck_contracts/v1/subjects.py", _SUBJECTS)
    _write(tmp_path / "services/alpha/src/alpha/main.py", _ALPHA)
    _write(tmp_path / "services/beta/src/beta/main.py", _BETA)
    _write(
        tmp_path / "services/alpha/alembic.ini",
        "[alembic]\nscript_location = %(here)s/src/alpha/migrations\n",
    )
    _write(tmp_path / "services/alpha/src/alpha/migrations/versions/0001_init.py", "")
    _write(tmp_path / "services/alpha/src/alpha/migrations/versions/0002_more.py", "")
    document = {
        "paths": {
            "/api/v1/alpha/things": {"get": {"tags": ["things"]}, "post": {"tags": ["things"]}},
            "/readyz": {"get": {"tags": ["operations"]}},
        }
    }
    _write(tmp_path / "apps/web/openapi/alpha.json", json.dumps(document))
    return tmp_path


def test_committed_page_is_fresh(repo_root: Path) -> None:
    generator = _generator(repo_root)
    assert not generator.stale(repo_root), (
        "docs/architecture/generated.md is stale: run `uv run poe arch-diagrams`"
    )


def test_compose_services_profiles_routes_and_externals(
    fixture_root: Path, repo_root: Path
) -> None:
    services = _generator(repo_root).load_compose(fixture_root)
    alpha, beta = services["alpha"], services["beta"]
    assert alpha.database == "alpha"
    assert alpha.nats
    assert {r.prefixes for r in alpha.routes} == {("/api/v1/alpha",), ()}
    assert {r.exact for r in alpha.routes} == {(), ("/api/v1/alpha/hook",)}
    assert alpha.http_peers == ("beta",)
    assert alpha.external == ("Stripe",)
    assert beta.profiles == ("extra",)
    assert services["alpha-migrate"].one_shot
    assert not services["nats"].own_image


def test_subjects_streams_and_bus_calls(fixture_root: Path, repo_root: Path) -> None:
    generator = _generator(repo_root)
    scope = generator.build_scope(fixture_root)
    streams = generator.load_streams(fixture_root)
    assert scope.constants["ALL_COMMANDS"] == "command.mower.*"
    assert [s.name for s in streams] == ["COMMANDS", "EVENTS"]
    usages, unresolved = generator.scan_usages(fixture_root, scope, streams)
    found = {(u.service, u.kind, u.subject) for u in usages}
    assert ("alpha", "publish", "command.mower.*") in found
    assert ("alpha", "subscribe", "event.beta.>") in found
    assert ("alpha", "request", "ping.request") in found
    assert ("beta", "subscribe", "command.mower.*") in found
    assert ("beta", "publish", "event.beta.done") in found
    assert unresolved == 1


def test_page_and_diagrams(fixture_root: Path, repo_root: Path) -> None:
    generator = _generator(repo_root)
    text, diagrams = generator.build(fixture_root)
    assert text == generator.build(fixture_root)[0]
    assert 'traefik -->|"/api/v1/alpha<br/>+1 exact path"| alpha' in diagrams["routing"]
    assert "traefik -.->" in diagrams["routing"]
    assert 'p_alpha -->|"command.mower.*"| s_COMMANDS' in diagrams["stream-commands"]
    assert 's_COMMANDS -->|"command.mower.*"| c_beta' in diagrams["stream-commands"]
    assert 'q_alpha -->|"ping.request"| r_beta' in diagrams["requests-beta"]
    assert "alpha_things" in diagrams["api"]
    assert "alpha_operations" not in diagrams["api"]
    assert "2 migrations" in diagrams["ownership"]
    assert text.count("```mermaid") == len(diagrams)


def test_check_flags_a_missing_or_stale_page(fixture_root: Path, repo_root: Path) -> None:
    generator = _generator(repo_root)
    assert generator.stale(fixture_root)
    assert generator.main(["--root", str(fixture_root)]) == 0
    assert not generator.stale(fixture_root)
    assert generator.main(["--root", str(fixture_root), "--check"]) == 0
    page = fixture_root / generator.PAGE
    page.write_text(page.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
    assert generator.main(["--root", str(fixture_root), "--check"]) == 1
