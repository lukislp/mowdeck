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


def test_e2e_override_only_adds_the_marker(repo_root: Path) -> None:
    """The e2e override adds the marker the suites check and changes no service of the stack."""
    compose_dir = _compose_dir(repo_root)
    base = yaml.safe_load((compose_dir / "docker-compose.yml").read_text("utf-8"))
    override = yaml.safe_load((compose_dir / "docker-compose.e2e.yml").read_text("utf-8"))
    assert set(override) == {"services"}
    assert set(override["services"]) == {"e2e-marker"}
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
