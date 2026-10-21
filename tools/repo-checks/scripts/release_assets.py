"""Select and check the files the release workflow attaches to a GitHub release.

The job ``publish`` downloads the artifacts of the run and attaches only these files:
``sbom-<image>.spdx.json``, the chart ``mowdeck-<version>.tgz``, the APK ``mowdeck-<version>.apk``
(when it was built), ``SOURCE-OFFER.md``, ``SHA256SUMS``, a ``<file>.sigstore.json`` bundle for
each of them and the build provenance ``mowdeck-<version>.intoto.jsonl``. Anything else an
action leaves among the artifacts (for example a Docker build record, ``*.dockerbuild``) is
ignored with a warning, so SHA256SUMS, the signatures and the provenance cover exactly the
release files.

    python tools/repo-checks/scripts/release_assets.py select --version 0.1.0 \
        --downloads downloads --assets assets --digests digests
    python tools/repo-checks/scripts/release_assets.py check --version 0.1.0 --assets assets
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

DIGEST = re.compile(r"^digest-[a-z0-9-]+\.txt$")
SBOM = re.compile(r"^sbom-[a-z0-9-]+\.spdx\.json$")


def selected(name: str, version: str) -> bool:
    """Whether a downloaded file is a release asset (before SHA256SUMS and the signatures)."""
    return bool(SBOM.match(name)) or name in {f"mowdeck-{version}.tgz", f"mowdeck-{version}.apk"}


def allowed(name: str, version: str) -> bool:
    """Whether a file may be attached to the release."""
    if name == f"mowdeck-{version}.intoto.jsonl":
        return True
    base = name.removesuffix(".sigstore.json")
    return selected(base, version) or base in {"SHA256SUMS", "SOURCE-OFFER.md"}


def select(version: str, downloads: Path, assets: Path, digests: Path) -> list[str]:
    """Move the digests and the release assets out of ``downloads``; return the ignored files."""
    assets.mkdir(parents=True, exist_ok=True)
    digests.mkdir(parents=True, exist_ok=True)
    ignored: list[str] = []
    for path in sorted(p for p in downloads.rglob("*") if p.is_file()):
        if path.parent == downloads and DIGEST.match(path.name):
            shutil.move(path, digests / path.name)
        elif path.parent == downloads and selected(path.name, version):
            shutil.move(path, assets / path.name)
        else:
            ignored.append(path.relative_to(downloads).as_posix())
    if not (assets / f"mowdeck-{version}.tgz").is_file():
        msg = f"the chart mowdeck-{version}.tgz is missing"
        raise SystemExit(msg)
    if not any(SBOM.match(p.name) for p in assets.iterdir()):
        msg = "no sbom-<image>.spdx.json"
        raise SystemExit(msg)
    return ignored


def check(version: str, assets: Path) -> list[str]:
    """Return the files in ``assets`` that must not be attached."""
    return sorted(
        p.name for p in assets.iterdir() if not (p.is_file() and allowed(p.name, version))
    )


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sel = sub.add_parser("select", help="move the release files out of the downloaded artifacts")
    sel.add_argument("--version", required=True)
    sel.add_argument("--downloads", type=Path, required=True)
    sel.add_argument("--assets", type=Path, required=True)
    sel.add_argument("--digests", type=Path, required=True)
    chk = sub.add_parser("check", help="fail if a file outside the allowed set would be attached")
    chk.add_argument("--version", required=True)
    chk.add_argument("--assets", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "select":
        for name in select(args.version, args.downloads, args.assets, args.digests):
            print(f"::warning::not a release asset, not attached: {name}")
        return 0
    unexpected = check(args.version, args.assets)
    for name in unexpected:
        print(f"::error::not a release asset: {name}")
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
