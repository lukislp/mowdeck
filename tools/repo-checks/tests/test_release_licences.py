"""Nothing a release publishes violates a licence (LICENSING.md, docs/ops/release.md).

Static half of the release licence guard; the release workflow's job ``licence-gate`` checks the
built images themselves (``scripts/licence_gate.py``, tested here too). Guarded:

* the Agora SDKs are proprietary and never redistributed: no workflow builds or pushes
  ``integrations/agora-bridge`` or the capture worker image (``vision-capture``, which contains
  the Agora Web SDK); the released Dockerfiles keep their Agora-free defaults
  (``WITH_CAPTURE=false``, ``WEB_AGORA_SDK=exclude``); no released Python image depends on an
  Agora package (uv.lock); the web build runs the bundle guard (apps/web/build-tools); no Agora
  binary is committed;
* every released image declares its licences (``org.opencontainers.image.licenses`` in its
  Dockerfile, equal to the gate's expectation) and carries the MowDeck licence texts; the
  GPL-3.0 adapter (mowdeck-control-mammotion) only ends up in device-gateway, whose label names
  GPL-3.0-only;
* every third-party image the GHCR mirror republishes is open source and its licence allows
  redistribution (a new image needs an entry in ``MIRROR_LICENCES`` after checking its licence).
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

AGORA = re.compile(r"agora", re.IGNORECASE)
AGORA_PACKAGE = re.compile(r"^(@agora-js/.+|agora([-_].+)?|libagora.*)$", re.IGNORECASE)
AGORA_FILE = re.compile(
    r"(libagora|AgoraRTC_N|agora[^/]*\.(so|dylib|dll|whl|zip|tgz|tar\.gz|aar|xcframework)$)",
    re.IGNORECASE,
)
GPL_ADAPTER = "mowdeck-control-mammotion"

# Third-party images under ghcr.io/<owner>/mowdeck/mirror (public packages): alias -> licence of
# the software in it. All are open source and allow verbatim redistribution; the GPL ones
# (busybox, the GNU parts of the Debian/Alpine bases) are copied unmodified with their upstream
# source offers. A new image fails here until its licence has been checked and added.
MIRROR_LICENCES = {
    "traefik": "MIT",
    "nats": "Apache-2.0",
    "postgres": "PostgreSQL",
    "openfga": "Apache-2.0",
    "whoami": "Apache-2.0",
    "mediamtx": "MIT",
    "python": "PSF-2.0",
    "uv": "MIT OR Apache-2.0",
    "node": "MIT",
    "nginx": "BSD-2-Clause",
    "busybox": "GPL-2.0-only",
    "fga-cli": "Apache-2.0",
    "jq": "MIT",
    "ntfy": "Apache-2.0 OR GPL-2.0-only",
    "stripe-mock": "MIT",
}

# Third-party images CI pulls from their own registry because the public mirror must not
# republish them: alias -> why. CI-only, never part of a release (no release workflow, chart or
# Compose file names them). A new entry here needs the same reasoning, not a licence shortcut.
NOT_MIRRORED = {
    # Playwright is Apache-2.0, Firefox MPL-2.0, WebKit LGPL-2.1/BSD, but the bundled Chromium is
    # Chrome for Testing ("Google Chrome ... All rights reserved", Google's terms, Widevine CDM).
    "playwright": "Apache-2.0 with Chrome for Testing (Google terms), MPL-2.0, LGPL-2.1",
}

# Non-comment Dockerfile lines that may name Agora: the switches that keep it out. The vision
# Dockerfile's capture build (WITH_CAPTURE=true, never released) is checked by its default.
DOCKERFILE_AGORA_LINES = {
    "apps/web/Dockerfile": {
        "ARG WEB_AGORA_SDK=exclude",
        'RUN MOWDECK_WEB_AGORA_SDK="$WEB_AGORA_SDK" pnpm --filter web build',
    },
}


def _workflow(repo_root: Path, name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(
        (repo_root / ".github" / "workflows" / name).read_text("utf-8")
    )
    return data


def _matrix(repo_root: Path, job: str) -> list[dict[str, str]]:
    jobs = _workflow(repo_root, "release.yml")["jobs"]
    include: list[dict[str, str]] = jobs[job]["strategy"]["matrix"]["include"]
    return include


def _code_lines(text: str) -> list[str]:
    """Lines that are not comments (YAML and Dockerfile ``#`` comments)."""
    return [line.strip() for line in text.splitlines() if not line.strip().startswith("#")]


def _gate(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "repo-checks" / "scripts" / "licence_gate.py"
    spec = importlib.util.spec_from_file_location("licence_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _closure(lock: dict[str, Any], root: str) -> set[str]:
    """Runtime packages of ``uv sync --no-dev --package <root>`` (markers ignored)."""
    packages = {pkg["name"]: pkg for pkg in lock["package"]}
    seen: set[str] = set()
    todo: list[tuple[str, tuple[str, ...]]] = [(root, ())]
    while todo:
        name, extras = todo.pop()
        key = f"{name}[{','.join(sorted(extras))}]"
        if key in seen:
            continue
        seen.add(key)
        pkg = packages[name]
        deps = list(pkg.get("dependencies", []))
        for extra in extras:
            deps += pkg.get("optional-dependencies", {}).get(extra, [])
        todo += [(dep["name"], tuple(dep.get("extra", []))) for dep in deps]
    return {key.split("[")[0] for key in seen}


def test_release_matrix_publishes_no_agora_image(repo_root: Path) -> None:
    images = _matrix(repo_root, "images")
    names = [entry["image"] for entry in images]
    for entry in images:
        assert not AGORA.search(json.dumps(entry)), f"release publishes {entry}: Agora SDK inside"
        assert (repo_root / entry["file"]).is_file(), entry["file"]
    assert "vision-capture" not in names, "vision-capture contains the Agora Web SDK"


def test_licence_gate_covers_every_released_image(repo_root: Path) -> None:
    keys = ("image", "context", "file", "kind")
    published = [{k: entry[k] for k in keys} for entry in _matrix(repo_root, "images")]
    gated = [{k: entry[k] for k in keys} for entry in _matrix(repo_root, "licence-gate")]
    assert gated == published, "the licence-gate matrix must equal the images matrix"
    release = _workflow(repo_root, "release.yml")["jobs"]
    assert "licence-gate" in release["images"]["needs"], "images must wait for the licence gate"
    labels = release["images"]["steps"]
    for step in labels:
        text = str((step.get("with") or {}).get("labels", ""))
        assert "image.licenses" not in text, "the licence label comes from each Dockerfile"


@pytest.mark.parametrize(
    "workflow", sorted(p.name for p in Path(__file__).parents[3].glob(".github/workflows/*.yml"))
)
def test_no_workflow_builds_agora_or_the_capture_image(repo_root: Path, workflow: str) -> None:
    text = (repo_root / ".github" / "workflows" / workflow).read_text("utf-8")
    for line in _code_lines(text):
        assert not AGORA.search(line), f"{workflow}: {line!r} (the Agora SDKs are never built)"
        assert "WITH_CAPTURE" not in line, f"{workflow}: {line!r} builds vision-capture"
        assert "vision-capture" not in line, f"{workflow}: {line!r}"


def test_released_dockerfiles_keep_agora_out(repo_root: Path) -> None:
    for entry in _matrix(repo_root, "images"):
        text = (repo_root / entry["file"]).read_text("utf-8")
        if entry["file"] == "services/vision/Dockerfile":
            continue
        allowed = DOCKERFILE_AGORA_LINES.get(entry["file"], set())
        for line in _code_lines(text):
            if AGORA.search(line):
                assert line in allowed, f"{entry['file']}: {line!r} names Agora"
    vision = (repo_root / "services/vision/Dockerfile").read_text("utf-8")
    assert vision.count("ARG WITH_CAPTURE=false") == 2, "vision: capture (Agora) off by default"
    web = (repo_root / "apps/web/Dockerfile").read_text("utf-8")
    assert "ARG WEB_AGORA_SDK=exclude" in web


def test_released_images_declare_their_licences(repo_root: Path) -> None:
    for entry in _matrix(repo_root, "licence-gate"):
        text = (repo_root / entry["file"]).read_text("utf-8")
        label = f'LABEL org.opencontainers.image.licenses="{entry["licenses"]}"'
        inline = f'org.opencontainers.image.licenses="{entry["licenses"]}"'
        assert label in text or inline in text, f"{entry['file']}: licence label {label}"
        if "--no-licence-files" in entry.get("flags", ""):
            continue
        assert "COPY LICENSE LICENSING.md /usr/share/doc/mowdeck/" in text, entry["file"]
        assert "COPY LICENSES/ /usr/share/doc/mowdeck/LICENSES/" in text, entry["file"]
        ignore = (repo_root / f"{entry['file']}.dockerignore").read_text("utf-8").splitlines()
        assert {"!LICENSE", "!LICENSING.md", "!LICENSES/"} <= set(ignore), entry["file"]


def test_python_images_contain_no_agora_and_gpl_only_where_declared(repo_root: Path) -> None:
    lock = tomllib.loads((repo_root / "uv.lock").read_text("utf-8"))
    for entry in _matrix(repo_root, "licence-gate"):
        if entry["kind"] != "python":
            continue
        closure = _closure(lock, entry["image"])
        agora = sorted(name for name in closure if AGORA_PACKAGE.match(name))
        assert not agora, f"{entry['image']} installs {agora}"
        assert "playwright" not in closure, f"{entry['image']}: the capture extra is not released"
        if GPL_ADAPTER in closure:
            assert "GPL-3.0-only" in entry["licenses"], f"{entry['image']}: GPL-3.0 adapter"
        else:
            assert not re.search(r"(?<!A)GPL-3\.0", entry["licenses"]), entry["image"]


def test_web_build_runs_the_bundle_guard(repo_root: Path) -> None:
    config = (repo_root / "apps/web/vite.config.ts").read_text("utf-8")
    assert "...releaseLicences(agora)" in config
    assert "agoraIncluded(process.env.MOWDECK_WEB_AGORA_SDK)" in config
    assert (repo_root / "apps/web/src/lib/camera/agora-player.excluded.ts").is_file()


def test_mirror_republishes_only_redistributable_images(repo_root: Path) -> None:
    out = subprocess.run(
        ["sh", "infra/mirror-env.sh", "--list"],  # noqa: S607
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    aliases = [line.split()[0] for line in out.splitlines() if line.strip()]
    unknown = sorted(set(aliases) - set(MIRROR_LICENCES))
    assert not unknown, f"mirror images without a checked licence: {unknown} (MIRROR_LICENCES)"
    stale = sorted(set(MIRROR_LICENCES) - set(aliases))
    assert not stale, f"MIRROR_LICENCES lists images the mirror no longer copies: {stale}"
    copied = sorted(set(aliases) & set(NOT_MIRRORED))
    assert not copied, f"the public mirror must not republish {copied} (NOT_MIRRORED)"


def test_unmirrored_images_stay_out_of_releases(repo_root: Path) -> None:
    released = [
        repo_root / ".github/workflows/release.yml",
        repo_root / "infra/compose/docker-compose.yml",
        repo_root / "infra/helm/mowdeck/values.yaml",
        repo_root / "infra/compose/.env.example",
    ]
    for alias in NOT_MIRRORED:
        pattern = re.compile(rf"[/\w.-]+/{re.escape(alias)}:", re.IGNORECASE)
        for path in released:
            text = path.read_text("utf-8")
            assert not pattern.search(text), f"{path.name} references the {alias} image"


def test_no_agora_binary_is_committed(repo_root: Path) -> None:
    if shutil.which("git") is None or not (repo_root / ".git").exists():
        pytest.skip("not a git checkout")
    files = subprocess.run(
        ["git", "ls-files"],  # noqa: S607
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    committed = [path for path in files if AGORA_FILE.search(path)]
    assert not committed, f"Agora SDK files in the repository: {committed}"


def test_helm_chart_ships_no_agora_bridge_image(repo_root: Path) -> None:
    values = yaml.safe_load((repo_root / "infra/helm/mowdeck/values.yaml").read_text("utf-8"))
    bridge = values["services"]["cameraRelay"]["agoraBridge"]
    assert bridge["enabled"] is False
    assert bridge["image"]["repository"] == "", "no default agora-bridge image (operator-built)"


# --- scripts/licence_gate.py -------------------------------------------------------------------


def _sbom(*artifacts: dict[str, Any]) -> dict[str, Any]:
    return {"artifacts": list(artifacts)}


def test_gate_flags_agora_packages_and_closed_licences(repo_root: Path) -> None:
    gate = _gate(repo_root)
    sbom = _sbom(
        {"type": "python", "name": "fastapi", "version": "1", "licenses": [{"value": "MIT"}]},
        {"type": "python", "name": "agora-python-server-sdk", "version": "2", "licenses": []},
        {"type": "python", "name": "agora", "version": "2", "licenses": []},
        {"type": "npm", "name": "@agora-js/media", "version": "4", "licenses": []},
        {"type": "npm", "name": "x", "version": "1", "licenses": [{"value": "SEE LICENSE IN a"}]},
        {"type": "deb", "name": "y", "version": "1", "licenses": [{"value": "Proprietary"}]},
        {"type": "deb", "name": "pagoda", "version": "1", "licenses": [{"value": "GPL-2.0"}]},
    )
    problems = gate.sbom_problems(sbom)
    assert problems == [
        "python:agora-python-server-sdk@2: Agora package",
        "python:agora@2: Agora package",
        "npm:@agora-js/media@4: Agora package",
        "npm:x@1: licence 'SEE LICENSE IN a' is not open source",
        "deb:y@1: licence 'Proprietary' is not open source",
    ]


def test_gate_flags_agora_files_but_not_mowdeck_or_pymammotion_code(repo_root: Path) -> None:
    gate = _gate(repo_root)
    paths = [
        "opt/agora-sdk/agora/agora_sdk/libagora_rtc_sdk.so",
        "app/.venv/lib/python3.13/site-packages/agora/rtc/agora_base.py",
        "usr/share/nginx/html/AgoraRTC_N-production.js",
        "app/node_modules/agora-rtc-sdk-ng/package.json",
        "app/.venv/lib/python3.13/site-packages/pymammotion/utility/agora_token.py",
        "app/.venv/lib/python3.13/site-packages/camera_relay/adapters/media/agora.py",
        "opt/agora/",
    ]
    assert gate.path_problems(paths) == [f"{path}: part of an Agora SDK" for path in paths[:4]]


def test_gate_checks_the_web_bundle(repo_root: Path) -> None:
    gate = _gate(repo_root)
    bundle = {
        "usr/share/nginx/html/_app/immutable/chunks/a.js": b"/* AgoraWebSDK_N-v4.24.8 */",
        "usr/share/nginx/html/_app/immutable/chunks/b.js": b"export const x = 1;",
        "usr/share/nginx/html/_app/immutable/chunks/c.js": b'class e{name="AgoraRTCException"}',
    }
    assert gate.bundle_problems(bundle) == [
        "usr/share/nginx/html/_app/immutable/chunks/a.js: contains the Agora Web SDK",
        "usr/share/nginx/html/_app/immutable/chunks/c.js: contains the Agora Web SDK",
    ]
    clean = json.dumps({"agoraWebSdk": False, "packages": [{"name": "svelte"}]}).encode()
    assert gate.web_manifest_problems(clean) == []
    dirty = json.dumps({"agoraWebSdk": True, "packages": [{"name": "agora-rtc-sdk-ng"}]}).encode()
    assert gate.web_manifest_problems(dirty) == [
        "web bundle built with MOWDECK_WEB_AGORA_SDK=include",
        "web bundle: agora-rtc-sdk-ng is an Agora package",
    ]
    missing = gate.web_manifest_problems(None)
    assert missing == [f"{gate.WEB_MANIFEST} missing (apps/web/build-tools/release-licences.ts)"]


def test_gate_main_checks_label_and_licence_files(repo_root: Path, tmp_path: Path) -> None:
    gate = _gate(repo_root)
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps(_sbom()), "utf-8")
    files = tmp_path / "files.txt"
    files.write_text("\n".join(gate.LICENCE_FILES), "utf-8")
    labels = tmp_path / "labels.json"
    expected = "AGPL-3.0-only AND Apache-2.0"
    labels.write_text(json.dumps({"org.opencontainers.image.licenses": expected}), "utf-8")
    base = ["--sbom", str(sbom), "--files", str(files), "--labels", str(labels)]
    assert gate.main([*base, "--licenses", expected]) == 0
    assert gate.main([*base, "--licenses", "AGPL-3.0-only"]) == 1
    files.write_text("usr/bin/python3\n", "utf-8")
    assert gate.main([*base, "--licenses", expected]) == 1
    assert gate.main([*base, "--licenses", expected, "--no-licence-files"]) == 0
