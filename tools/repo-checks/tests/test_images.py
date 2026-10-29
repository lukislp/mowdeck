"""The "Image sources" lines of infra/compose/.env.example are the one list of third-party images.

Every image default in the Dockerfiles (``ARG *_IMAGE=`` and literal ``FROM``) and in the Compose
stack (``${*_IMAGE:-...}``, literal ``image:``) must agree with that list. The CI workflows pull
every image of the list through the repository's GHCR mirror ``ghcr.io/<owner>/mowdeck/mirror/
<alias>:<tag>`` (alias: the variable name without ``_IMAGE``, lower case, ``-`` for ``_``), which
.github/workflows/mirror-images.yml fills from the same list through infra/mirror-env.sh. No
workflow pins an image of its own or pulls a listed image from its public registry, so the images
CI runs cannot drift from the ones the stack runs.
The one exception is ``UNMIRRORED``: an image whose licence does not allow the public mirror to
republish it, pinned by digest in its job instead.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ARG_RE = re.compile(r"^ARG\s+(\w+_IMAGE)=(\S+)\s*$", re.MULTILINE)
FROM_RE = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", re.MULTILINE)
VAR_DEFAULT_RE = re.compile(r"\$\{(\w+_IMAGE):-([^}]+)\}")
IMAGE_LINE_RE = re.compile(r"^\s*image:\s*(\S+)\s*$", re.MULTILINE)
ENV_LINE_RE = re.compile(r"^(\w+_IMAGE)=(\S*)\s*$", re.MULTILINE)
MIRROR = "ghcr.io/${{ github.repository_owner }}/mowdeck/mirror"
MIRROR_REF_RE = re.compile(r"mowdeck/mirror/([a-z0-9-]+):([\w.-]+)")
WORKFLOWS = ["ci.yml", "helm-kind.yml", "release.yml"]
MIRROR_WORKFLOW = "mirror-images.yml"
MIRROR_SCRIPT = "infra/mirror-env.sh"
# The only images pinned outside the list: (workflow, job, variable) -> registry prefix. They may
# not be republished by the public mirror (licence; test_release_licences.py, NOT_MIRRORED), so
# the job pulls them from their own registry, pinned by digest.
UNMIRRORED = {
    ("ci.yml", "e2e-web-shard", "PLAYWRIGHT_IMAGE"): "mcr.microsoft.com/playwright:",
    # The public demo's specs (ADR 0068) in the same image.
    ("ci.yml", "e2e-demo", "PLAYWRIGHT_IMAGE"): "mcr.microsoft.com/playwright:",
}


def _images(repo_root: Path) -> dict[str, str]:
    """Return variable -> image of the ``*_IMAGE=`` lines of infra/compose/.env.example."""
    text = (repo_root / "infra" / "compose" / ".env.example").read_text("utf-8")
    images: dict[str, str] = {}
    for name, value in ENV_LINE_RE.findall(text):
        assert name not in images, f".env.example: {name} listed twice"
        images[name] = value
    return images


def _alias(name: str) -> str:
    return name.removesuffix("_IMAGE").lower().replace("_", "-")


DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


def _digest(image: str) -> str:
    """Return the ``@sha256:...`` suffix of a digest-pinned image, else an empty string."""
    match = DIGEST_RE.search(image)
    return match.group(0) if match else ""


def _tag(image: str) -> str:
    last = image.removesuffix(_digest(image)).rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[-1] if ":" in last else ""


def _dockerfiles(repo_root: Path) -> list[Path]:
    files = [
        *sorted((repo_root / "services").glob("*/Dockerfile")),
        repo_root / "apps" / "web" / "Dockerfile",
        repo_root / "infra" / "openfga" / "bootstrap" / "Dockerfile",
        repo_root / "integrations" / "agora-bridge" / "Dockerfile",
        # The template's Dockerfile source is "{{ image_build_file }}.jinja" (templates/service/
        # copier.yml explains why it is not called Dockerfile.jinja).
        *sorted((repo_root / "templates").rglob("{{ image_build_file }}*.jinja")),
    ]
    return [f for f in files if f.is_file() and "dockerignore" not in f.name]


def _compose_files(repo_root: Path) -> list[Path]:
    return [
        repo_root / "infra" / "compose" / "docker-compose.yml",
        *sorted((repo_root / "services").glob("*/compose.snippet.yml")),
        *sorted((repo_root / "templates").rglob("compose.snippet.yml.jinja")),
    ]


def _workflow(repo_root: Path, name: str) -> tuple[str, dict[str, Any]]:
    path = repo_root / ".github" / "workflows" / name
    if not path.is_file():
        pytest.skip(f"{name} does not exist")
    text = path.read_text("utf-8")
    return text, yaml.safe_load(text)


def _triggers(data: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML reads the unquoted key ``on`` as the boolean True.
    triggers: dict[str, Any] = data.get("on") or data.get(True) or {}
    return triggers


def test_image_list_is_well_formed(repo_root: Path) -> None:
    images = _images(repo_root)
    assert images, "infra/compose/.env.example pins no *_IMAGE"
    aliases: dict[str, str] = {}
    for name, source in images.items():
        # A digest is optional and only after a tag (the mirror copies by tag).
        assert "@" not in source.removesuffix(_digest(source)), (
            f"{name}={source}: pin image:tag or image:tag@sha256:<64 hex digits>"
        )
        assert _tag(source), f"{name}={source} must pin a tag"
        assert _tag(source) != "latest", f"{name}={source} must not use :latest"
        alias = _alias(name)
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", alias), f"{name}: {alias!r} is no package name"
        assert alias not in aliases, f"{name} and {aliases[alias]} share the alias {alias}"
        aliases[alias] = name


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
            # Base images are pinned by digest as well (OpenSSF Scorecard, Pinned-Dependencies).
            assert _digest(default), f"{path}: ARG {name}={default} has no @sha256 digest"
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
            # mowdeck-local/: built on the operator's machine only (agora-bridge), never pulled.
            if image.startswith(("mowdeck/", "mowdeck-local/", "${")):
                continue
            assert image in sources, f"{path}: image {image} is not pinned in .env.example"


def test_env_example_covers_the_compose_variables(repo_root: Path) -> None:
    images = _images(repo_root)
    compose = (repo_root / "infra" / "compose" / "docker-compose.yml").read_text("utf-8")
    used = {name for name, _ in VAR_DEFAULT_RE.findall(compose)}
    assert used <= images.keys(), f".env.example misses {sorted(used - images.keys())}"


@pytest.mark.skipif(shutil.which("sh") is None, reason="sh is not available")
def test_mirror_env_script_derives_from_the_list(repo_root: Path) -> None:
    """infra/mirror-env.sh reads the same list and names the mirror images the same way."""
    images = _images(repo_root)
    sh = str(shutil.which("sh"))
    script = (repo_root / MIRROR_SCRIPT).as_posix()
    base = {k: v for k, v in os.environ.items() if k != "MOWDECK_IMAGE_MIRROR"}

    def run(*args: str, mirror: str | None = None) -> subprocess.CompletedProcess[str]:
        env = {**base, "MOWDECK_IMAGE_MIRROR": mirror} if mirror else base
        return subprocess.run(  # noqa: S603
            [sh, script, *args], capture_output=True, text=True, check=False, env=env
        )

    prefix = "registry.example.org/mowdeck"
    expected = [
        f"{name}={prefix}/{_alias(name)}:{_tag(source)}{_digest(source)}"
        for name, source in images.items()
    ]
    by_argument = run("Registry.Example.org/mowdeck/")  # lower-cased, trailing slash dropped
    assert by_argument.returncode == 0, by_argument.stderr
    assert by_argument.stdout.splitlines() == expected
    by_variable = run(mirror=prefix)
    assert by_variable.returncode == 0, by_variable.stderr
    assert by_variable.stdout.splitlines() == expected
    listing = run("--list")
    assert listing.returncode == 0, listing.stderr
    assert listing.stdout.splitlines() == [f"{_alias(n)} {s}" for n, s in images.items()]
    without_prefix = run()
    assert without_prefix.returncode != 0 and "usage" in without_prefix.stderr


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_workflows_pull_through_the_mirror(repo_root: Path, workflow: str) -> None:
    text, data = _workflow(repo_root, workflow)
    images = _images(repo_root)
    by_alias = {_alias(name): source for name, source in images.items()}
    assert (data.get("env") or {}).get("MOWDECK_IMAGE_MIRROR") == MIRROR, (
        f"{workflow}: env MOWDECK_IMAGE_MIRROR must be {MIRROR}"
    )
    assert f"sh {MIRROR_SCRIPT}" in text, f"{workflow}: the mirror references are not exported"
    assert "docker/login-action" in text and "secrets.GITHUB_TOKEN" in text, (
        f"{workflow}: no GHCR login with GITHUB_TOKEN"
    )
    # Only the list pins images: no workflow carries a version of its own.
    scopes = [("workflow env", data.get("env") or {})]
    scopes += [(f"job {name} env", job.get("env") or {}) for name, job in data["jobs"].items()]
    for scope, mapping in scopes:
        pinned = sorted(
            key
            for key in mapping
            if key.endswith("_IMAGE")
            and (workflow, scope.removeprefix("job ").removesuffix(" env"), key) not in UNMIRRORED
        )
        assert not pinned, f"{workflow}: {scope} pins {pinned}; pin images in .env.example"
    # A listed image is never pulled from its public registry.
    for name, source in images.items():
        assert source not in text, (
            f"{workflow}: pulls {source} directly; use ${name} exported by the mirror step"
        )
    for alias, tag in MIRROR_REF_RE.findall(text):
        assert alias in by_alias, f"{workflow}: mirror image {alias} is not in .env.example"
        assert _tag(by_alias[alias]) == tag, f"{workflow}: {alias}:{tag} != {by_alias[alias]}"
    for job_name, job in data["jobs"].items():
        for service, spec in (job.get("services") or {}).items():
            image = spec["image"]
            assert image.startswith(f"{MIRROR}/"), (
                f"{workflow}: job {job_name} service {service} runs {image}, not the mirror"
            )
            credentials = spec.get("credentials") or {}
            assert credentials.get("password") == "${{ secrets.GITHUB_TOKEN }}", (
                f"{workflow}: job {job_name} service {service} must log in with GITHUB_TOKEN"
            )


def test_packages_permission_matches_the_use(repo_root: Path) -> None:
    """ci and helm-kind read the mirror; release and mirror-images write packages."""
    for workflow in ("ci.yml", "helm-kind.yml"):
        _, data = _workflow(repo_root, workflow)
        assert data["permissions"].get("packages") == "read", f"{workflow}: packages: read"
        for name, job in data["jobs"].items():
            assert "packages" not in (job.get("permissions") or {}), f"{workflow}: job {name}"
    _, release = _workflow(repo_root, "release.yml")
    assert "packages" not in (release.get("permissions") or {})
    assert release["jobs"]["images"]["permissions"].get("packages") == "write"
    _, mirror = _workflow(repo_root, MIRROR_WORKFLOW)
    # Read-only at the top (OpenSSF Scorecard Token-Permissions), write on the copying job only.
    assert mirror["permissions"] == {"contents": "read"}
    assert mirror["jobs"]["mirror"]["permissions"] == {"contents": "read", "packages": "write"}


def test_mirror_workflow_fills_the_namespace_from_the_list(repo_root: Path) -> None:
    text, data = _workflow(repo_root, MIRROR_WORKFLOW)
    triggers = _triggers(data)
    assert "workflow_dispatch" in triggers, "the mirror must be fillable by hand"
    assert triggers["push"]["branches"] == ["main"]
    sources = {"infra/compose/.env.example", MIRROR_SCRIPT, f".github/workflows/{MIRROR_WORKFLOW}"}
    for trigger in ("push", "pull_request"):
        assert sources <= set(triggers[trigger]["paths"]), f"{trigger}: run when the list changes"
    assert data["concurrency"]["cancel-in-progress"] is False
    assert f"sh {MIRROR_SCRIPT} --list" in text, "the mirror must copy the list, not a copy of it"
    assert "/mowdeck/mirror" in text
    assert "docker/login-action" in text and "secrets.GITHUB_TOKEN" in text
    assert "crane digest" in text and "crane copy" in text


def test_kind_workflow_pushes_nothing(repo_root: Path) -> None:
    """The kind install test logs in to pull the mirror only; it builds and loads locally."""
    text, data = _workflow(repo_root, "helm-kind.yml")
    assert data["permissions"].get("packages") == "read", "read access to the mirror, nothing more"
    assert "push: true" not in text
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


def test_mirror_script_and_docs_name_the_namespace(repo_root: Path) -> None:
    readme = (repo_root / "infra" / "compose" / "README.md").read_text("utf-8")
    assert "mowdeck/mirror/<alias>:<tag>" in readme
    assert MIRROR_SCRIPT in readme and "MOWDECK_IMAGE_MIRROR" in readme
    env_example = (repo_root / "infra" / "compose" / ".env.example").read_text("utf-8")
    assert "mowdeck/mirror" in env_example


def test_unmirrored_images_are_pinned_and_unlisted(repo_root: Path) -> None:
    """The images the mirror must not republish stay out of the list and are digest-pinned."""
    images = _images(repo_root)
    for (workflow, job, name), prefix in UNMIRRORED.items():
        _, data = _workflow(repo_root, workflow)
        image = (data["jobs"][job].get("env") or {}).get(name, "")
        assert image.startswith(prefix), f"{workflow}: job {job} {name}={image!r} not {prefix}..."
        assert _tag(image) and _digest(image), f"{workflow}: {name}={image} needs tag and digest"
        assert name not in images, f"{name} is in .env.example: the mirror would republish it"
