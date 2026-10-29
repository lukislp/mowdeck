"""The GPL source offer of a release (scripts/source_offer.py, docs/ops/release.md)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml


@pytest.fixture(scope="module")
def offer(repo_root: Path) -> ModuleType:
    """The script as a module."""
    path = repo_root / "tools" / "repo-checks" / "scripts" / "source_offer.py"
    spec = importlib.util.spec_from_file_location("source_offer", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up while the class body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _package(name: str, purl: str, declared: str = "NOASSERTION") -> dict[str, Any]:
    return {
        "name": name,
        "licenseDeclared": declared,
        "licenseConcluded": "NOASSERTION",
        "externalRefs": [{"referenceType": "purl", "referenceLocator": purl}],
    }


SBOM_GATEWAY = [
    _package(
        "libc6",
        "pkg:deb/debian/libc6@2.41-12%2Bdeb13u4?arch=amd64&upstream=glibc&distro=debian-13.7",
        "BSD-2-Clause AND GPL-2.0-or-later AND LGPL-2.1-or-later",
    ),
    _package("bash", "pkg:deb/debian/bash@5.2.37-2%2Bb10?arch=amd64", "GPL-3.0-or-later"),
    _package("pymammotion", "pkg:pypi/pymammotion@0.5.9", "GPL-3.0-only"),
    _package(
        "mowdeck-control-mammotion", "pkg:pypi/mowdeck-control-mammotion@0.1.0", "GPL-3.0-only"
    ),
    _package("httpx", "pkg:pypi/httpx@0.28.1", "BSD-3-Clause"),
    _package("tzdata", "pkg:deb/debian/tzdata@2025b-4?arch=all", "LicenseRef-public-domain"),
]
SBOM_WEB = [
    _package(
        "busybox",
        "pkg:apk/alpine/busybox@1.37.0-r31?arch=x86_64&distro=alpine-3.24.2",
        "GPL-2.0-only",
    ),
    _package(
        "musl-utils",
        "pkg:apk/alpine/musl-utils@1.2.6-r2?arch=x86_64&upstream=musl&distro=alpine-3.24.2",
        "MIT AND GPL-2.0-or-later",
    ),
]
SBOM_BOOTSTRAP = [
    _package("busybox", "pkg:generic/busybox@1.37.0"),
    _package("jq", "pkg:generic/jq@1.8.1"),
    _package("libc6", "pkg:deb/debian/libc6@2.41-12%2Bdeb13u4?upstream=glibc", "LGPL-2.1"),
]


@pytest.fixture
def written(offer: ModuleType, repo_root: Path, tmp_path: Path) -> str:
    """SOURCE-OFFER.md for three fake SBOMs."""
    for image, packages in {
        "device-gateway": SBOM_GATEWAY,
        "web": SBOM_WEB,
        "authz-bootstrap": SBOM_BOOTSTRAP,
    }.items():
        document = {"spdxVersion": "SPDX-2.3", "packages": packages}
        (tmp_path / f"sbom-{image}.spdx.json").write_text(json.dumps(document), encoding="utf-8")
    out = tmp_path / "SOURCE-OFFER.md"
    sboms = sorted(str(p) for p in tmp_path.glob("sbom-*.spdx.json"))
    args = ["--version", "0.3.0", "--repository", "lukislp/mowdeck", "--out", str(out)]
    assert offer.main([*args, "--repo-root", str(repo_root), *sboms]) == 0
    return out.read_text(encoding="utf-8")


def _row(text: str, name: str, kind: str) -> str:
    rows = [line for line in text.splitlines() if line.startswith(f"| `{name}` ({kind})")]
    assert len(rows) == 1, (name, kind, rows)
    return rows[0]


def test_written_offer_with_contact_and_three_years(written: str) -> None:
    assert written.startswith("# MowDeck 0.3.0: source code offer")
    assert "at least three years" in written
    assert "https://github.com/lukislp/mowdeck/issues/new" in written
    assert "https://github.com/lukislp/mowdeck/archive/refs/tags/v0.3.0.tar.gz" in written
    # The contact is the repository's issue tracker or private reporting, no personal address.
    offer_section = written.split("## Written offer")[1].split("## Components")[0]
    assert "@" not in offer_section


def test_debian_packages_point_to_their_source_package(written: str) -> None:
    libc = _row(written, "libc6", "deb")
    assert "device-gateway" in libc
    assert "authz-bootstrap" in libc
    assert "https://snapshot.debian.org/package/glibc/2.41-12%2Bdeb13u4/" in libc
    # A binNMU (+b10) is built from the plain source version.
    assert "https://snapshot.debian.org/package/bash/5.2.37-2/" in _row(written, "bash", "deb")


def test_alpine_packages_point_to_aports_of_their_branch(written: str) -> None:
    musl = _row(written, "musl-utils", "apk")
    assert "`musl`" in musl
    assert "aports/-/tree/3.24-stable" in musl
    assert "web" in _row(written, "busybox", "apk")


def test_python_packages_own_and_third_party(written: str) -> None:
    assert "https://pypi.org/project/pymammotion/0.5.9/#files" in _row(
        written, "pymammotion", "pypi"
    )
    adapter = _row(written, "mowdeck-control-mammotion", "pypi")
    assert "https://github.com/lukislp/mowdeck/tree/v0.3.0" in adapter
    assert "`httpx`" not in written


def test_classified_busybox_binary_is_listed_and_jq_is_not(written: str) -> None:
    busybox = _row(written, "busybox", "generic")
    assert "GPL-2.0-only" in busybox
    assert "https://busybox.net/downloads/busybox-1.37.0.tar.bz2" in busybox
    assert "`jq`" not in written
    assert "`tzdata`" not in written


def test_release_workflow_attaches_the_signed_offer(repo_root: Path) -> None:
    workflow = yaml.safe_load((repo_root / ".github/workflows/release.yml").read_text("utf-8"))
    steps = workflow["jobs"]["publish"]["steps"]
    runs = "\n".join(step.get("run", "") for step in steps)
    offer_at = runs.index("source_offer.py")
    sums_at = runs.index("SHA256SUMS")
    sign_at = runs.index("cosign_sign_blob_verified")
    # Written before the checksums and the signatures, so both cover it.
    assert offer_at < sums_at < sign_at
    assert "assets/SOURCE-OFFER.md" in runs
