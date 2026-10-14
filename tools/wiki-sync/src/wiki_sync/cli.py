"""Command line entry point: ``wiki-sync build``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wiki_sync.core import build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wiki-sync", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="generate the wiki mirror from docs/")
    b.add_argument("--docs", type=Path, required=True, help="docs directory")
    b.add_argument("--mkdocs", type=Path, required=True, help="path to mkdocs.yml")
    b.add_argument("--out", type=Path, required=True, help="output directory")
    b.add_argument("--commit", default=None, help="commit sha to mention in the footer")
    args = parser.parse_args(argv)

    result = build(args.docs, args.mkdocs, args.out, args.commit)
    sys.stdout.write(
        f"wiki-sync: wrote {len(result.written)} pages to {args.out}, "
        f"removed {len(result.removed)} stale\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
