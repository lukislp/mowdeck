"""Guards for running released images with Compose (docs/ops/compose.md, "Updating to a release").

Every MowDeck service that a release publishes takes its image from
``${MOWDECK_IMAGE_REGISTRY:-mowdeck}/<image>:${MOWDECK_VERSION:-dev}``, so the stack runs the local
build by default and the signed GHCR images of a release with two variables. agora-bridge (Agora's
SDK) is never published and keeps a local image name.
``infra/compose/verify-release.sh`` is tested against fake ``gh``, ``curl``, ``docker`` and
``cosign`` commands, without network.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

PATTERN = "${{MOWDECK_IMAGE_REGISTRY:-mowdeck}}/{image}:${{MOWDECK_VERSION:-dev}}"
PREFIX = "${MOWDECK_IMAGE_REGISTRY:-mowdeck}/"
LOCAL_ONLY = {
    "agora-bridge": "mowdeck-local/agora-bridge:local",
}
REGISTRY = "ghcr.io/lukislp/mowdeck"
REPO = "lukislp/mowdeck"
IDENTITY = f"https://github.com/{REPO}/.github/workflows/release.yml@refs/heads/main"
VERSION = "0.1.1"
BASH = shutil.which("bash")
DOCKER = shutil.which("docker")


def _compose_dir(repo_root: Path) -> Path:
    return repo_root / "infra" / "compose"


def _services(repo_root: Path) -> dict[str, dict[str, Any]]:
    text = (_compose_dir(repo_root) / "docker-compose.yml").read_text("utf-8")
    services: dict[str, dict[str, Any]] = yaml.safe_load(text)["services"]
    return services


def _matrix_images(repo_root: Path, job: str) -> list[str]:
    text = (repo_root / ".github" / "workflows" / "release.yml").read_text("utf-8")
    include = yaml.safe_load(text)["jobs"][job]["strategy"]["matrix"]["include"]
    return [entry["image"] for entry in include]


def _released(repo_root: Path) -> dict[str, str]:
    """Compose service -> released image name, for every service on the registry pattern."""
    return {
        name: svc["image"].removeprefix(PREFIX).split(":", 1)[0]
        for name, svc in _services(repo_root).items()
        if svc.get("image", "").startswith(PREFIX)
    }


def test_every_built_service_uses_the_registry_pattern_or_is_local_only(repo_root: Path) -> None:
    released = _released(repo_root)
    for name, svc in _services(repo_root).items():
        if "build" not in svc:
            assert name not in released, f"{name}: the registry pattern needs a build section"
            continue
        if name in LOCAL_ONLY:
            assert svc["image"] == LOCAL_ONLY[name], f"{name} is never published"
            continue
        assert name in released, f"{name}: image must be {PATTERN.format(image='<image>')}"
        assert svc["image"] == PATTERN.format(image=released[name])


def test_compose_runs_exactly_the_released_images(repo_root: Path) -> None:
    """The images on the registry pattern are the release matrix, which the licence gate checks."""
    published = _matrix_images(repo_root, "images")
    assert sorted(_matrix_images(repo_root, "licence-gate")) == sorted(published)
    assert set(_released(repo_root).values()) == set(published)
    for image in LOCAL_ONLY:
        assert image not in published


def test_env_example_documents_the_image_variables(repo_root: Path) -> None:
    lines = (_compose_dir(repo_root) / ".env.example").read_text("utf-8").splitlines()
    assert "MOWDECK_IMAGE_REGISTRY=mowdeck" in lines
    assert "MOWDECK_VERSION=dev" in lines
    assert any(f"MOWDECK_IMAGE_REGISTRY={REGISTRY}" in line for line in lines)


def _profiles(repo_root: Path) -> list[str]:
    found = {p for svc in _services(repo_root).values() for p in svc.get("profiles", [])}
    return [arg for profile in sorted(found) for arg in ("--profile", profile)]


def _config(repo_root: Path, env: dict[str, str], *files: Path) -> dict[str, Any]:
    compose_dir = _compose_dir(repo_root)
    file_args = [
        arg for path in (compose_dir / "docker-compose.yml", *files) for arg in ("-f", str(path))
    ]
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "compose",
            *file_args,
            "--env-file",
            str(compose_dir / ".env.example"),
            *_profiles(repo_root),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
    )
    assert result.returncode == 0, result.stderr
    config: dict[str, Any] = json.loads(result.stdout)
    return config


@pytest.mark.skipif(DOCKER is None, reason="docker CLI not installed")
@pytest.mark.parametrize(
    ("registry", "version"), [("mowdeck", "dev"), (REGISTRY, VERSION)], ids=["local", "release"]
)
def test_compose_config_resolves_the_images(repo_root: Path, registry: str, version: str) -> None:
    config = _config(repo_root, {"MOWDECK_IMAGE_REGISTRY": registry, "MOWDECK_VERSION": version})
    services = config["services"]
    for name, image in _released(repo_root).items():
        assert services[name]["image"] == f"{registry}/{image}:{version}"
        assert "build" in services[name], f"{name}: `up --build` must keep working"
    assert services["agora-bridge"]["image"] == "mowdeck-local/agora-bridge:local"
    assert "vision-capture" not in services, "the capture worker is removed (ADR 0045)"


# --- verify-release.sh against fakes ------------------------------------------------------------

FAKE = {
    # Release notes: the gh fake prints FAKE_GH_BODY, the curl fake FAKE_RELEASE_JSON.
    "gh": """#!/usr/bin/env bash
echo "gh $*" >> "$FAKE_LOG"
[ -n "${FAKE_GH_BODY:-}" ] || exit 1
cat "$FAKE_GH_BODY"
""",
    "curl": """#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
[ -n "${FAKE_RELEASE_JSON:-}" ] || exit 22
cat "$FAKE_RELEASE_JSON"
""",
    # FAKE_TAGS: "<ref:tag> <digest>" lines; FAKE_SIGNED: "<ref@digest> <identity>" lines.
    "docker": """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
if [ "$1 $2 $3" = "buildx imagetools inspect" ]; then
  digest="$(awk -v r="$4" '$1 == r {print $2}' "$FAKE_TAGS")"
  [ -n "$digest" ] || exit 1
  printf '"%s"\\n' "$digest"
  exit 0
fi
if [ "$1 $2" = "run --rm" ]; then
  shift 3
  exec cosign "$@"
fi
exit 3
""",
    "cosign": """#!/usr/bin/env bash
echo "cosign $*" >> "$FAKE_LOG"
[ "$1" = verify ] || exit 3
ref="$2" identity="" issuer=""
shift 2
while [ "$#" -gt 0 ]; do
  case "$1" in
    --certificate-identity) identity="$2"; shift ;;
    --certificate-oidc-issuer) issuer="$2"; shift ;;
  esac
  shift
done
[ "$issuer" = https://token.actions.githubusercontent.com ] || exit 1
grep -qxF "$ref $identity" "$FAKE_SIGNED" || { echo "no matching signatures" >&2; exit 1; }
""",
}


def _digest(image: str) -> str:
    return "sha256:" + hashlib.sha256(image.encode()).hexdigest()


class Fakes:
    def __init__(self, repo_root: Path, tmp: Path) -> None:
        self.script = _compose_dir(repo_root) / "verify-release.sh"
        self.tmp = tmp
        self.images = _matrix_images(repo_root, "images")
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, body in FAKE.items():
            path = self.bin / name
            path.write_bytes(body.encode())
            path.chmod(0o755)
        self.log = tmp / "calls.log"
        self.log.touch()
        self.out = tmp / "docker-compose.release.yml"
        self.env_file = tmp / "env"
        self.env_file.write_text("", encoding="utf-8")
        self.digests = {image: _digest(image) for image in self.images}
        self.tags = {image: self.digests[image] for image in self.images}
        self.signed = dict.fromkeys(self.images, IDENTITY)
        self.notes: dict[str, str] = dict(self.digests)
        self.use_gh = False

    def body(self) -> str:
        rows = [f"| `{REGISTRY}/{image}` | `{digest}` |" for image, digest in self.notes.items()]
        rows.append(f"| `ghcr.io/lukislp/charts/mowdeck` | `{_digest('chart')}` |")
        return "## Images and chart\n\n| Artifact | Digest |\n| --- | --- |\n" + "\n".join(rows)

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        assert BASH is not None
        (self.tmp / "tags").write_text(
            "".join(f"{REGISTRY}/{i}:{VERSION} {d}\n" for i, d in self.tags.items()), "utf-8"
        )
        (self.tmp / "signed").write_text(
            "".join(f"{REGISTRY}/{i}@{self.digests[i]} {who}\n" for i, who in self.signed.items()),
            "utf-8",
        )
        (self.tmp / "release.json").write_text(
            json.dumps({"tag_name": f"v{VERSION}", "body": self.body()}), "utf-8"
        )
        (self.tmp / "body.md").write_text(self.body(), "utf-8")
        base = {
            key: value
            for key, value in os.environ.items()
            if key not in {"MOWDECK_IMAGE_REGISTRY", "SOURCE_URL", "MOWDECK_RELEASE_REPO"}
        }
        full_env = {
            **base,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "MOWDECK_ENV_FILE": str(self.env_file),
            "MOWDECK_COSIGN": "local",
            "FAKE_LOG": str(self.log),
            "FAKE_TAGS": str(self.tmp / "tags"),
            "FAKE_SIGNED": str(self.tmp / "signed"),
            "FAKE_RELEASE_JSON": str(self.tmp / "release.json"),
            "FAKE_GH_BODY": str(self.tmp / "body.md") if self.use_gh else "",
            **env,
        }
        return subprocess.run(  # noqa: S603
            [BASH, self.script.as_posix(), *args],
            capture_output=True,
            text=True,
            check=False,
            env=full_env,
        )

    def calls(self) -> str:
        return self.log.read_text("utf-8")


@pytest.fixture
def fakes(repo_root: Path, tmp_path: Path) -> Fakes:
    return Fakes(repo_root, tmp_path)


pytestmark_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")


@pytestmark_bash
def test_verified_release_is_pinned_by_digest(repo_root: Path, fakes: Fakes) -> None:
    result = fakes.run("--out", str(fakes.out), VERSION)
    assert result.returncode == 0, result.stderr
    override = yaml.safe_load(fakes.out.read_text("utf-8"))
    expected = {
        svc: {"image": f"{REGISTRY}/{image}:{VERSION}@{fakes.digests[image]}"}
        for svc, image in _released(repo_root).items()
    }
    assert override == {"services": expected}
    calls = fakes.calls()
    # No gh login: the public API, then every tag and every digest's signature.
    assert f"/repos/{REPO}/releases/tags/v{VERSION}" in calls
    for image in fakes.images:
        assert f"imagetools inspect {REGISTRY}/{image}:{VERSION} " in calls
        assert f"cosign verify {REGISTRY}/{image}@{fakes.digests[image]} " in calls
    assert "agora-bridge" not in fakes.out.read_text("utf-8")


@pytestmark_bash
def test_gh_is_used_when_it_works_and_v_prefix_is_accepted(fakes: Fakes) -> None:
    fakes.use_gh = True
    result = fakes.run("--print", f"v{VERSION}")
    assert result.returncode == 0, result.stderr
    assert f"gh release view v{VERSION} -R {REPO}" in fakes.calls()
    assert "curl" not in fakes.calls()
    assert f"{REGISTRY}/identity:{VERSION}@{fakes.digests['identity']}" in result.stdout


@pytestmark_bash
def test_cosign_container_when_cosign_is_not_installed(fakes: Fakes) -> None:
    result = fakes.run("--no-pin", VERSION, MOWDECK_COSIGN="container")
    assert result.returncode == 0, result.stderr
    assert "docker run --rm ghcr.io/sigstore/cosign/cosign:v3.0.5@sha256:" in fakes.calls()
    assert not fakes.out.exists()


def _assert_fails_closed(fakes: Fakes, result: subprocess.CompletedProcess[str], text: str) -> None:
    assert result.returncode == 1, result.stderr
    assert text in result.stderr
    assert "nothing pinned" in result.stderr or "verify-release:" in result.stderr
    assert not fakes.out.exists()
    assert list(fakes.out.parent.glob("docker-compose.release.yml*")) == []


@pytestmark_bash
def test_a_moved_tag_fails_closed(fakes: Fakes) -> None:
    fakes.tags["fleet"] = _digest("something else")
    result = fakes.run("--out", str(fakes.out), VERSION)
    _assert_fails_closed(fakes, result, "fleet: TAG MISMATCH")


@pytestmark_bash
def test_a_signature_of_another_workflow_fails_closed(fakes: Fakes) -> None:
    fakes.signed["web"] = (
        "https://github.com/someone/else/.github/workflows/release.yml@refs/heads/main"
    )
    result = fakes.run("--out", str(fakes.out), VERSION)
    _assert_fails_closed(fakes, result, "web: SIGNATURE INVALID")


@pytestmark_bash
def test_an_image_missing_from_the_release_fails_closed(fakes: Fakes) -> None:
    del fakes.notes["planner"]
    result = fakes.run("--out", str(fakes.out), VERSION)
    _assert_fails_closed(fakes, result, "planner: NOT IN RELEASE")


@pytestmark_bash
def test_an_unknown_release_fails_closed(fakes: Fakes) -> None:
    result = fakes.run("--out", str(fakes.out), VERSION, FAKE_RELEASE_JSON="")
    _assert_fails_closed(fakes, result, "not found")


@pytestmark_bash
def test_a_fork_verifies_against_its_own_repository(fakes: Fakes) -> None:
    fakes.env_file.write_text(
        "MOWDECK_IMAGE_REGISTRY=ghcr.io/somefork/mowdeck\n"
        "SOURCE_URL=https://github.com/SomeFork/mowdeck\n",
        "utf-8",
    )
    result = fakes.run("--out", str(fakes.out), VERSION)
    # The fakes only know lukislp's images, so verification fails, but against the fork.
    assert result.returncode == 1
    assert "signed by https://github.com/SomeFork/mowdeck/.github/workflows/release.yml" in (
        result.stderr
    )
    assert "/repos/SomeFork/mowdeck/releases/tags/" in fakes.calls()


@pytestmark_bash
@pytest.mark.parametrize(
    ("env", "text"),
    [
        ({"MOWDECK_IMAGE_REGISTRY": "mowdeck"}, "names local builds"),
        (
            {"SOURCE_URL": "https://github.com/someone/else"},
            "set SOURCE_URL or MOWDECK_RELEASE_REPO",
        ),
    ],
)
def test_misconfiguration_is_refused(fakes: Fakes, env: dict[str, str], text: str) -> None:
    result = fakes.run("--out", str(fakes.out), VERSION, **env)
    _assert_fails_closed(fakes, result, text)
    assert fakes.calls() == ""


@pytestmark_bash
@pytest.mark.parametrize("args", [(), ("latest",), ("--bogus", VERSION), (VERSION, VERSION)])
def test_bad_arguments(fakes: Fakes, args: tuple[str, ...]) -> None:
    result = fakes.run(*args)
    assert result.returncode in {1, 2}
    assert fakes.calls() == ""


@pytestmark_bash
@pytest.mark.skipif(DOCKER is None, reason="docker CLI not installed")
def test_the_pinned_override_is_a_valid_compose_file(repo_root: Path, fakes: Fakes) -> None:
    assert fakes.run("--out", str(fakes.out), VERSION).returncode == 0
    config = _config(repo_root, {"MOWDECK_IMAGE_REGISTRY": REGISTRY}, fakes.out)
    for name, image in _released(repo_root).items():
        assert config["services"][name]["image"] == (
            f"{REGISTRY}/{image}:{VERSION}@{fakes.digests[image]}"
        )


def test_verify_release_script_is_lf_and_executable(repo_root: Path) -> None:
    path = _compose_dir(repo_root) / "verify-release.sh"
    data = path.read_bytes()
    assert b"\r\n" not in data
    assert data.startswith(b"#!/usr/bin/env bash\n")
    assert b"set -euo pipefail" in data
    if shutil.which("git") is None:
        return
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo_root), "ls-files", "-s", "infra/compose/verify-release.sh"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout:
        assert result.stdout.startswith("100755"), "git must keep the executable bit"


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_verify_release_script_is_shellcheck_clean(repo_root: Path) -> None:
    result = subprocess.run(  # noqa: S603
        ["shellcheck", str(_compose_dir(repo_root) / "verify-release.sh")],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout
