"""Guards for the Docker Compose stack."""

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


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_compose_config_is_valid(repo_root: Path) -> None:
    compose_dir = _compose_dir(repo_root)
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "compose",
            "-f",
            str(compose_dir / "docker-compose.yml"),
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
