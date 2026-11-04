# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Compares two build outputs file by file (the reproducible build check).

Usage (CI, ``.github/workflows/reproducibility.yml``; docs/dev/reproducible-builds.md)::

    python3 tools/repo-checks/scripts/compare_builds.py <build-a> <build-b> \
        [--summary "$GITHUB_STEP_SUMMARY"] [--label artefacts]

Both directories are walked recursively. Every regular file is hashed with SHA-256 and the
two manifests (relative path -> hash) are compared: a file that exists on one side only or
whose bytes differ is a difference. Symbolic links count as files whose content is the link
target. There is no list of accepted exceptions: any difference fails the run (exit code 1),
so a new source of non-determinism shows up the first time it is introduced.

For OCI image layouts (``docker buildx build --output type=oci,tar=false``) this compares the
blobs, so an identical result also means identical manifest and config digests.

Only the standard library is used, so the script runs with the runner's ``python3``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path

CHUNK = 1 << 20


def _digest(path: Path) -> str:
    if path.is_symlink():
        return "symlink:" + os.readlink(path)
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            sha.update(chunk)
    return sha.hexdigest()


def manifest(root: Path) -> dict[str, str]:
    """``relative POSIX path -> SHA-256`` of every file below ``root``."""
    result: dict[str, str] = {}
    for directory, dirs, files in os.walk(root):
        dirs.sort()
        base = Path(directory)
        for name in sorted(files):
            path = base / name
            result[path.relative_to(root).as_posix()] = _digest(path)
        for name in dirs:
            path = base / name
            if path.is_symlink():
                result[path.relative_to(root).as_posix()] = _digest(path)
    return result


def compare(first: dict[str, str], second: dict[str, str]) -> list[tuple[str, str]]:
    """Sorted ``(path, reason)`` of every difference between two manifests."""
    differences = []
    for path in sorted(first.keys() | second.keys()):
        if path not in second:
            differences.append((path, "only in the first build"))
        elif path not in first:
            differences.append((path, "only in the second build"))
        elif first[path] != second[path]:
            differences.append((path, "content differs"))
    return differences


def report(
    label: str, first: dict[str, str], differences: Sequence[tuple[str, str]], total: int
) -> str:
    """The Markdown result for the job summary."""
    identical = total - len(differences)
    lines = [f"### Reproducible build: {label}", ""]
    if not differences:
        lines.append(f"**{identical}/{total} files bit-identical** between two clean builds.")
    else:
        lines.append(f"**{len(differences)} of {total} files differ** between two clean builds:")
        lines.append("")
        lines.append("| File | Difference |")
        lines.append("| --- | --- |")
        lines.extend(f"| `{path}` | {reason} |" for path, reason in differences)
    lines.append("")
    if first:
        digest = hashlib.sha256(
            "".join(f"{h}  {p}\n" for p, h in sorted(first.items())).encode()
        ).hexdigest()
        lines.append(f"Manifest SHA-256 of the first build: `{digest}`")
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("first", type=Path, help="output directory of the first build")
    parser.add_argument("second", type=Path, help="output directory of the second build")
    parser.add_argument("--label", default="build outputs", help="name in the summary")
    parser.add_argument("--summary", type=Path, help="append the Markdown result to this file")
    args = parser.parse_args(argv)

    for directory in (args.first, args.second):
        if not directory.is_dir():
            print(f"error: {directory} is not a directory", file=sys.stderr)
            return 2
    first = manifest(args.first)
    second = manifest(args.second)
    if not first and not second:
        print("error: both builds are empty", file=sys.stderr)
        return 2
    differences = compare(first, second)
    total = len(first.keys() | second.keys())
    text = report(args.label, first, differences, total)
    print(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
