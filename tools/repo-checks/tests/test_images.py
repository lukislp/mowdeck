"""The "Image sources" lines of infra/compose/.env.example are the one list of third-party images.

Every image default in the Dockerfiles (``ARG *_IMAGE=`` and literal ``FROM``), in the Compose
stack (``${*_IMAGE:-...}``, literal ``image:``) and in the CI workflows (service containers, the
CI-only images pinned in their ``env``) must agree with that list, so the images CI pulls from the
public registries cannot drift from the ones the stack runs. The workflows pull no package of the
repository's own: no image mirror, no registry login outside the release workflow.
"""

import re
from pathlib import Path

import pytest
import yaml

ARG_RE = re.compile(r"^ARG\s+(\w+_IMAGE)=(\S+)\s*$", re.MULTILINE)
FROM_RE = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", re.MULTILINE)
VAR_DEFAULT_RE = re.compile(r"\$\{(\w+_IMAGE):-([^}]+)\}")
IMAGE_LINE_RE = re.compile(r"^\s*image:\s*(\S+)\s*$", re.MULTILINE)
ENV_LINE_RE = re.compile(r"^(\w+_IMAGE)=(\S*)\s*$", re.MULTILINE)
WORKFLOWS = ["ci.yml", "helm-kind.yml", "release.yml"]


def _images(repo_root: Path) -> dict[str, str]:
    """Return variable -> image of the ``*_IMAGE=`` lines of infra/compose/.env.example."""
    text = (repo_root / "infra" / "compose" / ".env.example").read_text("utf-8")
    images: dict[str, str] = {}
    for name, value in ENV_LINE_RE.findall(text):
        assert name not in images, f".env.example: {name} listed twice"
        images[name] = value
    return images


def _tag(image: str) -> str:
    last = image.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[-1] if ":" in last else ""


def _dockerfiles(repo_root: Path) -> list[Path]:
    files = [
        *sorted((repo_root / "services").glob("*/Dockerfile")),
        repo_root / "apps" / "web" / "Dockerfile",
        repo_root / "infra" / "openfga" / "bootstrap" / "Dockerfile",
        *sorted((repo_root / "templates").rglob("Dockerfile*.jinja")),
    ]
    return [f for f in files if f.is_file() and "dockerignore" not in f.name]


def _compose_files(repo_root: Path) -> list[Path]:
    return [
        repo_root / "infra" / "compose" / "docker-compose.yml",
        *sorted((repo_root / "services").glob("*/compose.snippet.yml")),
        *sorted((repo_root / "templates").rglob("compose.snippet.yml.jinja")),
    ]


def _workflow(repo_root: Path, name: str) -> tuple[str, dict]:
    path = repo_root / ".github" / "workflows" / name
    if not path.is_file():
        pytest.skip(f"{name} does not exist")
    text = path.read_text("utf-8")
    return text, yaml.safe_load(text)


def test_image_list_is_well_formed(repo_root: Path) -> None:
    images = _images(repo_root)
    assert images, "infra/compose/.env.example pins no *_IMAGE"
    for name, source in images.items():
        assert "@" not in source, f"{name}={source}: pin a tag, not a digest"
        assert _tag(source), f"{name}={source} must pin a tag"
        assert _tag(source) != "latest", f"{name}={source} must not use :latest"


def test_dockerfiles_use_listed_base_images(repo_root: Path) -> None:
    images = _images(repo_root)
    sources = set(images.values())
    checked = 0
    for path in _dockerfiles(repo_root):
        text = path.read_text("utf-8")
        args = dict(ARG_RE.findall(text))
        for name, default in args.items():
            assert images.get(name) == default, (
                f"{path}: ARG {name}={default} differs from .env.example "
                f"({images.get(name, 'not listed')})"
            )
            checked += 1
        stages = {m.group(1) for m in re.finditer(r"\bAS\s+(\S+)", text, re.IGNORECASE)}
        for base in FROM_RE.findall(text):
            if base.startswith("${") or base in stages or base == "scratch":
                continue
            assert base in sources, f"{path}: FROM {base} is not pinned in .env.example"
            checked += 1
    assert checked >= 10


def test_compose_defaults_use_listed_images(repo_root: Path) -> None:
    images = _images(repo_root)
    sources = set(images.values())
    for path in _compose_files(repo_root):
        text = path.read_text("utf-8")
        for name, default in VAR_DEFAULT_RE.findall(text):
            assert images.get(name) == default, (
                f"{path}: ${{{name}:-{default}}} differs from .env.example "
                f"({images.get(name, 'not listed')})"
            )
        for image in IMAGE_LINE_RE.findall(text):
            if image.startswith(("mowdeck/", "${")):
                continue
            assert image in sources, f"{path}: image {image} is not pinned in .env.example"


def test_env_example_covers_the_compose_variables(repo_root: Path) -> None:
    images = _images(repo_root)
    compose = (repo_root / "infra" / "compose" / "docker-compose.yml").read_text("utf-8")
    used = {name for name, _ in VAR_DEFAULT_RE.findall(compose)}
    assert used <= images.keys(), f".env.example misses {sorted(used - images.keys())}"


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_workflow_images_are_pinned_and_listed(repo_root: Path, workflow: str) -> None:
    """Service containers and the CI-only images of the workflows agree with the list."""
    text, data = _workflow(repo_root, workflow)
    images = _images(repo_root)
    sources = set(images.values())
    for name, value in (data.get("env") or {}).items():
        if not name.endswith("_IMAGE"):
            continue
        assert "${{" not in str(value), f"{workflow}: env {name} must be a literal image"
        assert _tag(str(value)) not in {"", "latest"}, f"{workflow}: env {name}={value} not pinned"
        if name in images:
            assert images[name] == value, f"{workflow}: env {name}={value} != {images[name]}"
    for job_name, job in data["jobs"].items():
        for service, spec in (job.get("services") or {}).items():
            image = spec["image"]
            assert image in sources, (
                f"{workflow}: job {job_name} service {service} runs {image}, "
                "which .env.example does not pin"
            )
    assert "infra/compose/.env.example" in text, f"{workflow}: the image list is not used"


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_workflows_use_no_image_mirror(repo_root: Path, workflow: str) -> None:
    text, data = _workflow(repo_root, workflow)
    assert "mowdeck-mirror" not in text and "mirror-env" not in text, (
        f"{workflow}: the images come from the public registries, not from a mirror"
    )
    if workflow != "release.yml":
        assert "packages" not in (data.get("permissions") or {}), (
            f"{workflow}: only release.yml needs the packages permission"
        )


def test_kind_workflow_pushes_nothing(repo_root: Path) -> None:
    """The kind install test builds locally and loads the images into the node."""
    text, data = _workflow(repo_root, "helm-kind.yml")
    assert "docker/login-action" not in text
    kind_test = (repo_root / "infra" / "helm" / "kind-test.sh").read_text("utf-8")
    assert "kind load docker-image" in kind_test
    for job in data["jobs"].values():
        for step in job["steps"]:
            if "docker/build-push-action" in str(step.get("uses", "")):
                assert step["with"].get("push") is False, f"{step['name']}: must not push"
                assert step["with"].get("load") is True, f"{step['name']}: must load"
    values = yaml.safe_load(
        (repo_root / "infra" / "helm" / "mowdeck" / "ci" / "kind-values.yaml").read_text("utf-8")
    )
    assert values["image"]["pullPolicy"] in {"Never", "IfNotPresent"}
