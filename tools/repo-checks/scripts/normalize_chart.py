"""Rewrite a Helm chart package with its entries in a fixed order.

Helm 4 writes the files of the subcharts (NATS, OpenFGA and OpenFGA's own dependency) in map
order, which changes from run to run, so two ``helm package`` runs of the same commit give
different archives although every file and every time stamp is the same. This script sorts the
entries by path and writes the tar again, with the gzip header's time and name left empty, so the
package depends on its content only (docs/dev/reproducible-builds.md). The release workflow and
reproducible_build.sh run it right after ``helm package``; Helm reads the result like any chart.

Usage: python3 normalize_chart.py <chart.tgz> [...]
"""

from __future__ import annotations

import argparse
import gzip
import io
import tarfile
from pathlib import Path


def normalize(path: Path) -> None:
    """Sort the entries of the gzipped tar at ``path`` by name; write it back without gzip time."""
    entries: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(path, "r:gz") as source:
        for member in source.getmembers():
            data = None
            if member.isfile():
                extracted = source.extractfile(member)
                if extracted is None:
                    raise ValueError(f"{path}: cannot read {member.name}")
                data = extracted.read()
            entries.append((member, data))
    entries.sort(key=lambda entry: entry[0].name)

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.PAX_FORMAT) as target:
        for member, data in entries:
            target.addfile(member, io.BytesIO(data) if data is not None else None)

    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0) as stream:
        stream.write(archive.getvalue())
    path.write_bytes(compressed.getvalue())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("packages", nargs="+", type=Path, help="chart packages (.tgz)")
    for package in parser.parse_args(argv).packages:
        normalize(package)
        print(f"normalized {package}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
