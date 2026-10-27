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


class _ComposeLoader(yaml.SafeLoader):
    """Reads Compose's ``!reset`` tag (it drops a value of the base file)."""


def _reset(loader: yaml.SafeLoader, node: yaml.SequenceNode) -> dict[str, list[object]]:
    return {"!reset": loader.construct_sequence(node)}


_ComposeLoader.add_constructor("!reset", _reset)


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
    # The aerial photo (ADR 0053) against a fake WMS: Telemetry only gets the source variables.
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
