# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Coverage of a CI run as a Markdown table for the job summary (``$GITHUB_STEP_SUMMARY``).

Usage (CI, ``.github/workflows/ci.yml``)::

    uv run coverage json -q -o coverage.json
    python3 tools/repo-checks/scripts/coverage_summary.py --python coverage.json
    python3 tools/repo-checks/scripts/coverage_summary.py \
        --vitest apps/web apps/web/coverage/coverage-summary.json

``--python`` reads the report of ``coverage json`` (pytest-cov with branch coverage; test code
is omitted in ``[tool.coverage.run]`` of the root ``pyproject.toml``, so the numbers are those of
the product code). ``--vitest`` reads the ``json-summary`` report of Vitest's v8 coverage, which
covers the modules listed under ``coverage.include`` of that package's ``vite.config.ts``.

Only the standard library is used, so the script runs with the runner's ``python3``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _pct(covered: int, total: int) -> str:
    return "n/a" if total == 0 else f"{covered / total * 100:.1f} %"


def python_rows(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(metric, percent, covered/total)`` rows of a ``coverage json`` report."""
    totals = report["totals"]
    statements = int(totals["num_statements"])
    covered = int(totals["covered_lines"])
    branches = int(totals.get("num_branches", 0))
    covered_branches = int(totals.get("covered_branches", 0))
    rows = [
        ("Statements", _pct(covered, statements), f"{covered}/{statements}"),
        ("Branches", _pct(covered_branches, branches), f"{covered_branches}/{branches}"),
    ]
    combined_total = statements + branches
    rows.append(
        (
            "Combined (coverage.py total)",
            _pct(covered + covered_branches, combined_total),
            f"{covered + covered_branches}/{combined_total}",
        )
    )
    return rows


def vitest_rows(summary: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(metric, percent, covered/total)`` rows of a Vitest ``json-summary`` report."""
    total = summary["total"]
    rows = []
    for key in ("statements", "branches", "functions", "lines"):
        entry = total[key]
        rows.append(
            (
                key.capitalize(),
                _pct(int(entry["covered"]), int(entry["total"])),
                f"{entry['covered']}/{entry['total']}",
            )
        )
    return rows


def table(title: str, rows: list[tuple[str, str, str]], note: str) -> str:
    """A Markdown section with one table."""
    lines = [f"### {title}", "", "| Metric | Coverage | Covered/total |", "| --- | --- | --- |"]
    lines += [f"| {metric} | {pct} | {count} |" for metric, pct, count in rows]
    lines += ["", note, ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python", type=Path, help="report of `coverage json`")
    parser.add_argument(
        "--vitest",
        nargs=2,
        metavar=("PACKAGE", "SUMMARY"),
        help="package name and its Vitest json-summary report",
    )
    args = parser.parse_args(argv)
    if args.python is None and args.vitest is None:
        parser.error("give --python or --vitest")
    out: list[str] = []
    if args.python is not None:
        report = json.loads(args.python.read_text("utf-8"))
        out.append(
            table(
                "Python coverage (product code, tests omitted)",
                python_rows(report),
                "Measured by `uv run poe test` (pytest-cov, branch coverage on).",
            )
        )
    if args.vitest is not None:
        package, path = args.vitest
        summary = json.loads(Path(path).read_text("utf-8"))
        out.append(
            table(
                f"{package} coverage (Vitest, v8)",
                vitest_rows(summary),
                f"Modules listed under `coverage.include` in `{package}/vite.config.ts`.",
            )
        )
    sys.stdout.write("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
