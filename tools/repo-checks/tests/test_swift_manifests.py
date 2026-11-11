# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Swift package manifests start with their tools-version line.

SwiftPM reads ``// swift-tools-version: X.Y`` only from the first line of ``Package.swift``. With
anything above it (a licence header, for instance) Xcode refuses to resolve the packages:
"the tools-version was specified in a subsequent line of the manifest, not the first line". No
pull request job builds the iOS app, so only the TestFlight run would notice. The SPDX header
therefore goes below that line.
"""

import re
import subprocess
from pathlib import Path

TOOLS_VERSION = re.compile(r"^// swift-tools-version: ?\d+\.\d+(\.\d+)?$")

LICENCE_TAG = "SPDX-License-" + "Identifier:"
"""Written in two parts: REUSE reads the tag as a licence statement wherever it finds it whole."""


def _manifests(repo_root: Path) -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "--", "*Package.swift"],  # noqa: S607 - git from PATH, as CI has it
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [repo_root / name for name in listed if Path(name).name == "Package.swift"]


def test_the_repository_has_swift_manifests(repo_root: Path) -> None:
    """The check below must not pass because the search found nothing."""
    assert _manifests(repo_root)


def test_every_swift_manifest_starts_with_its_tools_version(repo_root: Path) -> None:
    wrong = []
    for manifest in _manifests(repo_root):
        first = manifest.read_text("utf-8").split("\n", 1)[0].rstrip("\r")
        if not TOOLS_VERSION.match(first):
            wrong.append(f"{manifest.relative_to(repo_root).as_posix()}: {first!r}")
    assert not wrong, (
        "the first line of a Package.swift must be its '// swift-tools-version: X.Y' line "
        f"(put the SPDX header below it): {wrong}"
    )


def test_every_swift_manifest_keeps_its_licence_header(repo_root: Path) -> None:
    """Moving the header down must not lose it (REUSE)."""
    for manifest in _manifests(repo_root):
        head = manifest.read_text("utf-8").split("\n")[:4]
        assert any(LICENCE_TAG in line for line in head), manifest
