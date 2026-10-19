"""Licence gate of a release image: nothing proprietary or Agora-made is published.

The release workflow builds every image once for linux/amd64 (loaded into the local Docker
engine, never pushed) and runs, before anything is pushed::

    python3 tools/repo-checks/scripts/licence_gate.py --image <tag> --sbom <syft.json> \
        --licenses "<expected org.opencontainers.image.licenses>"

It fails (exit 1) when

* a package of the Syft SBOM (syft-json) is an Agora package (``agora-rtc-sdk-ng``,
  ``@agora-js/*``, ``agora-*``, the ``agora`` wheel of the Agora Python server SDK,
  ``libagora*``) or declares a licence that is not open source (``SEE LICENSE IN``,
  ``UNLICENSED``, proprietary, commercial, ...);
* a file of the image's file system is part of an Agora SDK (``libagora*``, ``agora_sdk/``,
  ``AgoraRTC_N*``, ``node_modules/agora-rtc-sdk-ng``, ``node_modules/@agora-js``) or a web
  bundle file contains the Agora Web SDK (the SBOM cannot see bundled JavaScript);
* the web image's ``third-party-licences.json`` (apps/web/build-tools/release-licences.ts) is
  missing or says the build includes the Agora Web SDK;
* the image's ``org.opencontainers.image.licenses`` label differs from ``--licenses``, or the
  MowDeck licence texts are missing under ``/usr/share/doc/mowdeck`` (``--no-licence-files``
  for an image whose build context cannot reach them).

Standard library only; ``docker`` must be on the PATH. ``--files`` / ``--labels`` take a file
list and a label JSON instead of asking Docker (tests).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tarfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

AGORA_PACKAGE = re.compile(r"^(@agora-js/.+|agora([-_].+)?|libagora.*)$", re.IGNORECASE)
NON_OSS_LICENCE = re.compile(
    r"SEE[\s-]+LICEN[CS]E|UNLICENSED|propriet|commercial|non-?free|all rights reserved|"
    r"LicenseRef-Agora|Agora",
    re.IGNORECASE,
)
AGORA_PATH = re.compile(
    r"(^|/)(libagora[^/]*|agora_sdk|agora_rtc_sdk[^/]*|AgoraRTC_N[^/]*|agora-rtc-sdk-ng[^/]*|"
    r"@agora-js|agora-sdk)(/|$)|(^|/)site-packages/agora/",
    re.IGNORECASE,
)
# Markers of the Agora Web SDK inside a bundled JavaScript file.
AGORA_BUNDLE_MARKERS = (
    b"AgoraWebSDK_N",
    b"AgoraRTC_N-production",
    b"agora-rtc-sdk-ng",
    b"AgoraRTCClient",
    b"AgoraRTCException",
)
LICENCE_FILES = (
    "usr/share/doc/mowdeck/LICENSE",
    "usr/share/doc/mowdeck/LICENSING.md",
    "usr/share/doc/mowdeck/LICENSES/AGPL-3.0.txt",
    "usr/share/doc/mowdeck/LICENSES/Apache-2.0.txt",
    "usr/share/doc/mowdeck/LICENSES/GPL-3.0.txt",
)
WEB_ROOT = "usr/share/nginx/html/"
WEB_MANIFEST = WEB_ROOT + "third-party-licences.json"


def _licences(artifact: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for entry in artifact.get("licenses") or []:
        if isinstance(entry, dict):
            value = entry.get("spdxExpression") or entry.get("value")
            if value:
                values.append(str(value))
        elif entry:
            values.append(str(entry))
    return values


def sbom_problems(sbom: dict[str, Any]) -> list[str]:
    """Agora packages and non open source licences of a syft-json SBOM."""
    problems: list[str] = []
    for artifact in sbom.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        ident = f"{artifact.get('type')}:{artifact.get('name')}@{artifact.get('version')}"
        if AGORA_PACKAGE.match(str(artifact.get("name", ""))):
            problems.append(f"{ident}: Agora package")
        problems.extend(
            f"{ident}: licence {licence!r} is not open source"
            for licence in _licences(artifact)
            if NON_OSS_LICENCE.search(licence)
        )
    return problems


def path_problems(paths: Iterable[str]) -> list[str]:
    """Files of an Agora SDK."""
    return [f"{path}: part of an Agora SDK" for path in paths if AGORA_PATH.search(path)]


def bundle_problems(files: dict[str, bytes]) -> list[str]:
    """Web bundle files (path -> content) that contain the Agora Web SDK."""
    return [
        f"{path}: contains the Agora Web SDK"
        for path, content in files.items()
        if any(marker in content for marker in AGORA_BUNDLE_MARKERS)
    ]


def web_manifest_problems(manifest: bytes | None) -> list[str]:
    """The web build's third-party list: present and without the Agora Web SDK."""
    if manifest is None:
        return [f"{WEB_MANIFEST} missing (apps/web/build-tools/release-licences.ts)"]
    data = json.loads(manifest)
    problems = []
    if data.get("agoraWebSdk") is not False:
        problems.append("web bundle built with MOWDECK_WEB_AGORA_SDK=include")
    problems.extend(
        f"web bundle: {pkg.get('name')} is an Agora package"
        for pkg in data.get("packages", [])
        if AGORA_PACKAGE.match(str(pkg.get("name", "")))
    )
    return problems


def label_problems(labels: dict[str, str] | None, expected: str) -> list[str]:
    """The licence label of the image config."""
    actual = (labels or {}).get("org.opencontainers.image.licenses")
    if actual != expected:
        return [f"label org.opencontainers.image.licenses is {actual!r}, expected {expected!r}"]
    return []


def licence_file_problems(paths: Iterable[str]) -> list[str]:
    """The MowDeck licence texts inside the image."""
    present = set(paths)
    return [f"{path} missing" for path in LICENCE_FILES if path not in present]


def _docker(*args: str, check: bool = True) -> str:
    """Run ``docker <args>`` (the image tag comes from the workflow) and return its stdout."""
    return subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        check=check,
        capture_output=True,
        text=True,
    ).stdout


def _export(image: str) -> Iterator[tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]]:
    """Members of the image's file system; web bundle files with their content."""
    container = _docker("create", image).strip()
    try:
        with subprocess.Popen(  # noqa: S603
            ["docker", "export", container],  # noqa: S607
            stdout=subprocess.PIPE,
        ) as proc:
            if proc.stdout is None:
                raise RuntimeError("docker export has no output")
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                for member in tar:
                    name = member.name.lstrip("./")
                    if member.isfile() and name.startswith(WEB_ROOT):
                        extracted = tar.extractfile(member)
                        content = extracted.read() if extracted else b""
                        yield member, content
                    else:
                        yield member
    finally:
        _docker("rm", container, check=False)


def _labels(image: str) -> dict[str, str]:
    out = _docker("image", "inspect", "--format", "{{json .Config.Labels}}", image)
    labels: dict[str, str] = json.loads(out) or {}
    return labels


def main(argv: list[str] | None = None) -> int:
    """Run the gate; print every problem and return 1 if there is one."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", help="local image to inspect (docker create/export)")
    parser.add_argument("--sbom", type=Path, required=True, help="syft-json SBOM of the image")
    parser.add_argument("--licenses", required=True, help="expected licence label")
    parser.add_argument("--web", action="store_true", help="the web image (bundle checks)")
    parser.add_argument("--no-licence-files", action="store_true")
    parser.add_argument("--files", type=Path, help="file list instead of --image (tests)")
    parser.add_argument("--labels", type=Path, help="label JSON instead of --image (tests)")
    args = parser.parse_args(argv)

    problems = sbom_problems(json.loads(args.sbom.read_text("utf-8")))
    paths: list[str] = []
    bundle: dict[str, bytes] = {}
    if args.files:
        paths = [line.strip().lstrip("./") for line in args.files.read_text("utf-8").splitlines()]
    elif args.image:
        for item in _export(args.image):
            if isinstance(item, tuple):
                member, content = item
                bundle[member.name.lstrip("./")] = content
                paths.append(member.name.lstrip("./"))
            else:
                paths.append(item.name.lstrip("./"))
    else:
        parser.error("--image or --files is required")
    problems += path_problems(paths)
    if args.web:
        problems += bundle_problems(bundle)
        problems += web_manifest_problems(bundle.get(WEB_MANIFEST))
    if not args.no_licence_files:
        problems += licence_file_problems(paths)
    if args.labels:
        labels = json.loads(args.labels.read_text("utf-8"))
    elif args.image:
        labels = _labels(args.image)
    else:
        labels = {}
    problems += label_problems(labels, args.licenses)

    target = args.image or str(args.files)
    if problems:
        print(f"licence gate FAILED for {target}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"licence gate passed for {target} ({len(paths)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
