"""Guards for the Docker Compose stack."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


def _compose_dir(repo_root: Path) -> Path:
    return repo_root / "infra" / "compose"


def test_compose_file_parses(repo_root: Path) -> None:
    data = yaml.safe_load((_compose_dir(repo_root) / "docker-compose.yml").read_text("utf-8"))
    assert isinstance(data, dict)
    assert "services" in data
    for image in (svc.get("image", "") for svc in data["services"].values()):
        assert ":" in image, f"image {image!r} must pin a tag"
        assert not image.endswith(":latest"), f"image {image!r} must not use :latest"


def test_postgres_is_healthy_only_once_it_accepts_tcp(repo_root: Path) -> None:
    # The init scripts run on a socket-only server; the migrations connect over TCP.
    data = yaml.safe_load((_compose_dir(repo_root) / "docker-compose.yml").read_text("utf-8"))
    test = " ".join(data["services"]["postgres"]["healthcheck"]["test"])
    assert "pg_isready -h 127.0.0.1 " in test


class _ComposeLoader(yaml.SafeLoader):
    """Reads Compose's ``!reset`` tag (it drops a value of the base file) and ``!override``
    (it replaces the value of the base file instead of merging)."""


def _reset(loader: yaml.SafeLoader, node: yaml.SequenceNode) -> dict[str, list[object]]:
    return {"!reset": loader.construct_sequence(node)}


def _override(loader: yaml.SafeLoader, node: yaml.SequenceNode) -> dict[str, list[object]]:
    return {"!override": loader.construct_sequence(node)}


_ComposeLoader.add_constructor("!reset", _reset)
_ComposeLoader.add_constructor("!override", _override)


def test_e2e_override_adds_the_marker_and_the_relay_with_test_hooks(repo_root: Path) -> None:
    """The e2e override adds the marker the suites check, enables the camera relay with its
    test hooks, loosens Identity's login and registration rate limit (the whole suite runs
    from one address) and points Identity's GitHub and Apple sign-in at fakes of their own; it
    changes no other service of the stack."""
    compose_dir = _compose_dir(repo_root)
    base = yaml.safe_load((compose_dir / "docker-compose.yml").read_text("utf-8"))
    text = (compose_dir / "docker-compose.e2e.yml").read_text("utf-8")
    override = yaml.load(text, Loader=_ComposeLoader)  # noqa: S506 - a SafeLoader subclass
    assert set(override) == {"services", "volumes"}
    assert set(override["volumes"]) == {"fake-apple-keys"}
    assert set(override["services"]) == {
        "e2e-marker",
        "camera-relay",
        "mediamtx",
        "identity",
        "fake-github",
        "fake-apple",
        "fake-wms",
        "telemetry",
    }
    identity = override["services"]["identity"]
    assert identity["labels"] == [
        "traefik.http.middlewares.identity-ratelimit.ratelimit.average=300",
        "traefik.http.middlewares.identity-ratelimit.ratelimit.burst=100",
    ]
    # GitHub sign-in (ADR 0051) only against the fake GitHub of the test stack.
    assert set(identity) == {"labels", "environment", "volumes", "depends_on"}
    assert identity["environment"]["MOWDECK_IDENTITY_GITHUB_TOKEN_URL"].startswith(
        "http://fake-github:8000/"
    )
    assert identity["environment"]["MOWDECK_IDENTITY_GITHUB_API_URL"].startswith(
        "http://fake-github:8000/"
    )
    # Sign in with Apple (ADR 0052) likewise, with the key the fake generates into a volume.
    for name in ("TOKEN", "KEYS"):
        assert identity["environment"][f"MOWDECK_IDENTITY_APPLE_{name}_URL"].startswith(
            "http://fake-apple:8000/"
        )
    assert identity["depends_on"] == {"fake-apple": {"condition": "service_healthy"}}
    assert "fake-github" not in base["services"] and "fake-apple" not in base["services"]
    # The aerial photo (ADR 0054) against a fake WMS: Telemetry only gets the source variables.
    telemetry = override["services"]["telemetry"]
    assert set(telemetry) == {"environment", "depends_on"}
    assert (
        telemetry["environment"]["MOWDECK_TELEMETRY_AERIAL_WMS_URL"] == "http://fake-wms:8000/wms"
    )
    assert "fake-wms" not in base["services"]
    base_env = base["services"]["identity"]["environment"]
    assert base_env["MOWDECK_IDENTITY_GITHUB_CLIENT_ID"] == "${IDENTITY_GITHUB_CLIENT_ID:-}"
    assert not any("_GITHUB_" in k and k.endswith("_URL") for k in base_env)
    assert base_env["MOWDECK_IDENTITY_APPLE_SERVICES_ID"] == "${IDENTITY_APPLE_SERVICES_ID:-}"
    assert not any("_APPLE_" in k and k.endswith("_URL") for k in base_env)
    # The regular stack keeps its production limit.
    identity_labels = base["services"]["identity"]["labels"]
    assert "traefik.http.middlewares.identity-ratelimit.ratelimit.average=10" in identity_labels
    assert "traefik.http.middlewares.identity-ratelimit.ratelimit.burst=20" in identity_labels
    relay, mediamtx = override["services"]["camera-relay"], override["services"]["mediamtx"]
    assert relay == {
        "profiles": {"!reset": []},
        "environment": {"MOWDECK_RELAY_TEST_HOOKS": "true"},
    }
    assert mediamtx == {"profiles": {"!reset": []}}
    # The regular stack never serves the test hooks.
    assert "MOWDECK_RELAY_TEST_HOOKS" not in base["services"]["camera-relay"]["environment"]
    assert "e2e-marker" not in base["services"]
    marker = override["services"]["e2e-marker"]
    assert marker["hostname"] == "${COMPOSE_PROJECT_NAME}"
    assert (
        "traefik.http.routers.e2e-marker.rule=PathPrefix(`/api/v1/e2e-stack`)" in marker["labels"]
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
@pytest.mark.parametrize(
    "files", [("docker-compose.yml",), ("docker-compose.yml", "docker-compose.e2e.yml")]
)
def test_compose_config_is_valid(repo_root: Path, files: tuple[str, ...]) -> None:
    compose_dir = _compose_dir(repo_root)
    file_args = [arg for name in files for arg in ("-f", str(compose_dir / name))]
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "compose",
            *file_args,
            "--env-file",
            str(compose_dir / ".env.example"),
            "config",
            "-q",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_traefik_trusts_forwarded_headers_only_from_configured_proxies(repo_root: Path) -> None:
    """GATEWAY_TRUSTED_PROXIES sets the trusted IPs of both entry points; empty trusts no one."""
    compose_dir = _compose_dir(repo_root)
    data = yaml.safe_load((compose_dir / "docker-compose.yml").read_text("utf-8"))
    command = data["services"]["traefik"]["command"]
    for entrypoint in ("web", "websecure"):
        prefix = f"--entrypoints.{entrypoint}.forwardedHeaders."
        flags = [f for f in command if f.startswith(prefix)]
        assert flags == [
            f"--entrypoints.{entrypoint}.forwardedHeaders.trustedIPs=${{GATEWAY_TRUSTED_PROXIES:-}}"
        ]
        # insecure mode would trust every client's X-Forwarded-For
        assert not any("forwardedHeaders.insecure" in f for f in command)
    env_example = (compose_dir / ".env.example").read_text("utf-8").splitlines()
    assert "GATEWAY_TRUSTED_PROXIES=" in env_example


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
@pytest.mark.parametrize(
    ("value", "expected"), [("", ""), ("192.0.2.10,198.51.100.0/24", "192.0.2.10,198.51.100.0/24")]
)
def test_trusted_proxies_reach_the_traefik_command(
    repo_root: Path, value: str, expected: str
) -> None:
    compose_dir = _compose_dir(repo_root)
    env = {**os.environ, "GATEWAY_TRUSTED_PROXIES": value}
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "compose",
            "-f",
            str(compose_dir / "docker-compose.yml"),
            "--env-file",
            str(compose_dir / ".env.example"),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    command = json.loads(result.stdout)["services"]["traefik"]["command"]
    for entrypoint in ("web", "websecure"):
        assert f"--entrypoints.{entrypoint}.forwardedHeaders.trustedIPs={expected}" in command


def test_only_the_whep_endpoints_of_mediamtx_are_routed(repo_root: Path) -> None:
    """Traefik routes /media/whep/r/ (prefix stripped) to MediaMTX's WebRTC server and nothing
    else of it: no API, metrics, playback or RTSP route, and no host port but the ICE port."""
    data = yaml.safe_load((_compose_dir(repo_root) / "docker-compose.yml").read_text("utf-8"))
    mediamtx = data["services"]["mediamtx"]
    labels = mediamtx["labels"]
    assert labels == [
        "traefik.enable=true",
        "traefik.http.routers.mediamtx-whep.rule=PathPrefix(`/media/whep/r/`)",
        "traefik.http.routers.mediamtx-whep.entrypoints=web",
        "traefik.http.routers.mediamtx-whep.middlewares=mediamtx-whep-strip",
        "traefik.http.middlewares.mediamtx-whep-strip.stripprefix.prefixes=/media/whep",
        "traefik.http.services.mediamtx-whep.loadbalancer.server.port=8889",
    ]
    ice = "${RELAY_ICE_BIND:-0.0.0.0}:${RELAY_ICE_PORT:-8189}:${RELAY_ICE_PORT:-8189}"
    assert mediamtx["ports"] == [f"{ice}/udp", f"{ice}/tcp"]
    env = mediamtx["environment"]
    assert env["MTX_AUTHHTTPADDRESS"] == "http://camera-relay:8000/internal/mediamtx/auth"
    assert env["MTX_WEBRTCLOCALUDPADDRESS"] == env["MTX_WEBRTCLOCALTCPADDRESS"]
    assert env["MTX_WEBRTCLOCALUDPADDRESS"] == ":${RELAY_ICE_PORT:-8189}"
    assert mediamtx["profiles"] == ["relay"]
    assert "ports" not in data["services"]["camera-relay"]


def test_the_agora_bridge_is_built_locally_and_lives_in_the_relays_namespace(
    repo_root: Path,
) -> None:
    """agora-bridge contains Agora's proprietary SDK (ADR 0045 step 7): opt-in profile, built
    from integrations/agora-bridge and never pulled, no port and no network of its own (it
    listens on camera-relay's loopback, which the relay always names)."""
    data = yaml.safe_load((_compose_dir(repo_root) / "docker-compose.yml").read_text("utf-8"))
    bridge = data["services"]["agora-bridge"]
    assert bridge["profiles"] == ["agora"]
    assert bridge["pull_policy"] == "build"
    assert bridge["image"].startswith("mowdeck-local/")
    assert bridge["build"]["context"] == "../../integrations/agora-bridge"
    assert bridge["network_mode"] == "service:camera-relay"
    assert "ports" not in bridge and "networks" not in bridge and "labels" not in bridge
    assert bridge["read_only"] is True and bridge["cap_drop"] == ["ALL"]
    assert bridge["environment"]["AGORA_BRIDGE_LISTEN"] == "127.0.0.1:7700"
    relay = data["services"]["camera-relay"]["environment"]
    assert relay["MOWDECK_RELAY_AGORA_BRIDGE_URL"] == "tcp://127.0.0.1:7700"
    assert "agora-bridge" not in yaml.safe_dump(data["services"]["traefik"])


def test_no_workflow_builds_or_pushes_the_agora_bridge(repo_root: Path) -> None:
    """The bridge image is never built in CI or published (it would contain Agora's SDK)."""
    for workflow in (repo_root / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text("utf-8")
        assert "integrations/agora-bridge/Dockerfile" not in text, workflow.name
        assert "context: integrations/agora-bridge" not in text, workflow.name
        assert "agora-bridge:local" not in text, workflow.name


def test_the_mediamtx_config_asks_the_relay_and_serves_nothing_else(repo_root: Path) -> None:
    config = yaml.safe_load(
        (_compose_dir(repo_root) / "mediamtx" / "mediamtx.yml").read_text("utf-8")
    )
    assert config["authMethod"] == "http"
    assert config["authHTTPExclude"] == []
    for server in ("metrics", "pprof", "playback", "rtmp", "hls", "srt", "moq"):
        assert config[server] is False, server
    assert config["webrtcIPsFromInterfaces"] is False
    assert config["webrtcICEServers2"] == []
    assert config["rtspTransports"] == ["tcp"]
    assert list(config["paths"]) == ["~^r/[0-9a-f]{32}/[A-Za-z0-9_-]{1,64}(/src)?$"]
    assert "all_others" not in config["paths"]


def test_demo_override_puts_every_service_in_demo_mode(repo_root: Path) -> None:
    """The public demo (ADR 0068): every MowDeck service of the base stack runs with
    MOWDECK_DEMO_MODE, Traefik keeps the base command plus the demo middlewares on both entry
    points, the seed job runs before the Device-Gateway, and no optional profile is enabled."""
    compose_dir = _compose_dir(repo_root)
    base = yaml.safe_load((compose_dir / "docker-compose.yml").read_text("utf-8"))
    text = (compose_dir / "docker-compose.demo.yml").read_text("utf-8")
    demo = yaml.load(text, Loader=_ComposeLoader)  # noqa: S506 - a SafeLoader subclass
    services = demo["services"]
    mowdeck_services = {
        name
        for name, svc in base["services"].items()
        if str(svc.get("image", "")).startswith("${MOWDECK_IMAGE_REGISTRY:-mowdeck}/")
        and "profiles" not in svc
        and not name.endswith("-migrate")
        # One-shot jobs without an HTTP API: the OpenFGA model and the JetStream streams.
        and name not in {"web", "authz-bootstrap", "nats-streams"}
    }
    assert mowdeck_services, "no MowDeck services found in the base stack"
    for name in sorted(mowdeck_services):
        environment = services.get(name, {}).get("environment", {})
        assert environment.get("MOWDECK_DEMO_MODE") == "true", name
        assert "MOWDECK_DEMO_SEED_TOKEN" in environment, name
    middlewares = "demo-headers@docker,demo-ratelimit@docker"
    assert services["traefik"]["command"] == [
        *base["services"]["traefik"]["command"][:8],
        f"--entrypoints.web.http.middlewares={middlewares}",
        f"--entrypoints.websecure.http.middlewares={middlewares}",
        *base["services"]["traefik"]["command"][8:],
    ]
    labels = services["web"]["labels"]
    assert (
        "traefik.http.middlewares.demo-headers.headers.customresponseheaders.X-MowDeck-Demo=1"
        in labels
    )
    assert (
        "traefik.http.middlewares.demo-headers.headers.customrequestheaders.X-MowDeck-Demo-Seed="
        in labels
    )
    assert services["web"]["environment"]["MOWDECK_WEB_DEMO_MODE"] == "true"
    assert services["device-gateway"]["depends_on"] == {
        "demo-seed": {"condition": "service_completed_successfully"}
    }
    assert services["fleet"]["environment"]["MOWDECK_ALLOWED_CONNECTION_KINDS"] == "simulated"
    assert services["identity"]["volumes"] == {"!reset": []}
    assert not any("profiles" in svc for svc in services.values())
    assert (compose_dir / "demo" / "seed.py").is_file()


def test_demo_publishes_only_the_gateway_http_port(repo_root: Path) -> None:
    """The demo publishes the gateway's HTTP entry point and nothing else: no websecure port,
    no NATS (unauthenticated), Postgres or OpenFGA port on the host; the base stack keeps its
    ports."""
    compose_dir = _compose_dir(repo_root)
    base = yaml.safe_load((compose_dir / "docker-compose.yml").read_text("utf-8"))["services"]
    text = (compose_dir / "docker-compose.demo.yml").read_text("utf-8")
    services = yaml.load(text, Loader=_ComposeLoader)["services"]  # noqa: S506
    assert services["traefik"]["ports"] == {"!override": ["${GATEWAY_HTTP_PORT:-80}:80"]}
    for name in ("nats", "postgres", "openfga"):
        assert base[name]["ports"], name
        assert services[name]["ports"] == {"!reset": []}, name
    published = {name for name, svc in base.items() if svc.get("ports")}
    allowed = {"traefik", "nats", "postgres", "openfga"}
    # Services with profiles (mediamtx) never run in a demo (reset.sh refuses profiles).
    assert {name for name in published - allowed if "profiles" not in base[name]} == set()
    example = (compose_dir / "demo" / "demo.env.example").read_text("utf-8").splitlines()
    for variable in (
        "GATEWAY_HTTPS_PORT",
        "POSTGRES_PORT",
        "NATS_CLIENT_PORT",
        "NATS_MONITOR_PORT",
        "OPENFGA_HTTP_PORT",
        "OPENFGA_GRPC_PORT",
    ):
        assert not any(line.startswith(f"{variable}=") for line in example), variable


def test_every_rate_limit_counts_the_configured_client_entry(repo_root: Path) -> None:
    """Every Traefik rate limit of every Compose file counts the X-Forwarded-For entry of
    GATEWAY_CLIENT_IP_DEPTH (default 0: the TCP peer, as without a source criterion), so
    behind a trusted proxy each client has its own bucket, and an IPv6 client per /64
    (docs/ops/demo.md)."""
    compose_dir = _compose_dir(repo_root)
    depth = "${GATEWAY_CLIENT_IP_DEPTH:-0}"
    seen = set()
    for path in sorted(compose_dir.glob("docker-compose*.yml")):
        data = yaml.load(path.read_text("utf-8"), Loader=_ComposeLoader)  # noqa: S506
        labels = [
            label
            for service in data["services"].values()
            for label in service.get("labels", [])
            if isinstance(label, str)
        ]
        prefix = "traefik.http.middlewares."
        limits = {
            label.removeprefix(prefix).split(".")[0]
            for label in labels
            if label.startswith(prefix) and ".ratelimit." in label
        }
        defined = {
            name
            for name in limits
            if f"{prefix}{name}.ratelimit.period=1m" in labels
            or any(label.startswith(f"{prefix}{name}.ratelimit.average=") for label in labels)
        }
        for name in defined:
            if path.name == "docker-compose.e2e.yml":
                continue  # only loosens limits of the base file
            assert f"{prefix}{name}.ratelimit.sourcecriterion.ipstrategy.depth={depth}" in labels, (
                f"{path.name}: {name} has no source criterion"
            )
            # IPv6: one bucket per /64 (what one connection gets), not per address.
            assert f"{prefix}{name}.ratelimit.sourcecriterion.ipstrategy.ipv6subnet=64" in labels, (
                f"{path.name}: {name} counts every IPv6 address of a /64 separately"
            )
            seen.add(name)
    assert {
        "identity-ratelimit",
        "identity-session-ratelimit",
        "billing-webhook-ratelimit",
        "demo-ratelimit",
    } <= seen
    env_example = (compose_dir / ".env.example").read_text("utf-8").splitlines()
    assert "GATEWAY_CLIENT_IP_DEPTH=" in env_example
    base = yaml.safe_load((compose_dir / "docker-compose.yml").read_text("utf-8"))["services"]
    for service in ("identity", "telemetry"):
        env = base[service]["environment"]
        assert env["MOWDECK_CLIENT_IP_DEPTH"] == "${GATEWAY_CLIENT_IP_DEPTH:-}", service
