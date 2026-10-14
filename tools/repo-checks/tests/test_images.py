"""infra/images.txt is the single list of third-party images.

Every image default in the Dockerfiles (``ARG *_IMAGE=`` and literal ``FROM``), in the Compose
stack (``${*_IMAGE:-...}``, literal ``image:``, ``.env.example``) and the mirror references in
the CI workflows must come from that list, so the GHCR mirror CI pulls from cannot drift.
"""

import re
from pathlib import Path

import pytest

ARG_RE = re.compile(r"^ARG\s+(\w+_IMAGE)=(\S+)\s*$", re.MULTILINE)
FROM_RE = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", re.MULTILINE)
VAR_DEFAULT_RE = re.compile(r"\$\{(\w+_IMAGE):-([^}]+)\}")
IMAGE_LINE_RE = re.compile(r"^\s*image:\s*(\S+)\s*$", re.MULTILINE)
ENV_LINE_RE = re.compile(r"^(\w+_IMAGE)=(\S*)\s*$", re.MULTILINE)
MIRROR_REF_RE = re.compile(r"mowdeck-mirror/([a-z0-9-]+):([\w.-]+)")


def _images(repo_root: Path) -> dict[str, str]:
    """Return alias -> source image of infra/images.txt."""
    images: dict[str, str] = {}
    for raw in (repo_root / "infra" / "images.txt").read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        alias, source = line.split()
        assert alias not in images, f"alias {alias} listed twice"
        images[alias] = source
    return images


def _variable(alias: str) -> str:
    return alias.upper().replace("-", "_") + "_IMAGE"


def _by_variable(repo_root: Path) -> dict[str, str]:
    return {_variable(alias): source for alias, source in _images(repo_root).items()}


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


def test_image_list_is_well_formed(repo_root: Path) -> None:
    images = _images(repo_root)
    assert images, "infra/images.txt lists no image"
    for alias, source in images.items():
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", alias), f"alias {alias!r}: lower case, digits, -"
        assert "@" not in source, f"{source}: use a tag, the mirror keeps the tag"
        tag = source.rsplit(":", 1)[-1] if ":" in source.rsplit("/", 1)[-1] else ""
        assert tag, f"{source} must pin a tag"
        assert tag != "latest", f"{source} must not use :latest"


def test_dockerfiles_use_listed_base_images(repo_root: Path) -> None:
    by_variable = _by_variable(repo_root)
    sources = set(by_variable.values())
    checked = 0
    for path in _dockerfiles(repo_root):
        text = path.read_text("utf-8")
        args = dict(ARG_RE.findall(text))
        for name, default in args.items():
            assert by_variable.get(name) == default, (
                f"{path}: ARG {name}={default} differs from infra/images.txt "
                f"({by_variable.get(name, 'not listed')})"
            )
            checked += 1
        stages = {m.group(1) for m in re.finditer(r"\bAS\s+(\S+)", text, re.IGNORECASE)}
        for base in FROM_RE.findall(text):
            if base.startswith("${") or base in stages or base == "scratch":
                continue
            assert base in sources, f"{path}: FROM {base} is not listed in infra/images.txt"
            checked += 1
    assert checked >= 10


def test_compose_defaults_use_listed_images(repo_root: Path) -> None:
    by_variable = _by_variable(repo_root)
    sources = set(by_variable.values())
    for path in _compose_files(repo_root):
        text = path.read_text("utf-8")
        for name, default in VAR_DEFAULT_RE.findall(text):
            assert by_variable.get(name) == default, (
                f"{path}: ${{{name}:-{default}}} differs from infra/images.txt "
                f"({by_variable.get(name, 'not listed')})"
            )
        for image in IMAGE_LINE_RE.findall(text):
            if image.startswith(("mowdeck/", "${")):
                continue
            assert image in sources, f"{path}: image {image} is not listed in infra/images.txt"


def test_env_example_matches_the_list(repo_root: Path) -> None:
    by_variable = _by_variable(repo_root)
    text = (repo_root / "infra" / "compose" / ".env.example").read_text("utf-8")
    env = dict(ENV_LINE_RE.findall(text))
    compose = (repo_root / "infra" / "compose" / "docker-compose.yml").read_text("utf-8")
    used = {name for name, _ in VAR_DEFAULT_RE.findall(compose)}
    assert used <= env.keys(), f".env.example misses {sorted(used - env.keys())}"
    for name, value in env.items():
        assert by_variable.get(name) == value, f".env.example: {name}={value} differs from the list"


@pytest.mark.parametrize("workflow", ["ci.yml"])
def test_ci_mirror_references_are_listed(repo_root: Path, workflow: str) -> None:
    path = repo_root / ".github" / "workflows" / workflow
    if not path.is_file():
        pytest.skip(f"{workflow} does not exist")
    images = _images(repo_root)
    for alias, tag in MIRROR_REF_RE.findall(path.read_text("utf-8")):
        assert alias in images, f"{workflow}: mirror image {alias} is not in infra/images.txt"
        assert images[alias].endswith(f":{tag}"), f"{workflow}: {alias}:{tag} != {images[alias]}"
