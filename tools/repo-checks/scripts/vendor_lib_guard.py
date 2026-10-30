"""Vendor library updates wait for a hardware test (docs/ops/vendor-lib-updates.md).

The adapters talk to real mowers through one vendor library each (``pymammotion``,
``pyworxcloud``, ``aioautomower``). A bug in a new release of one of them can break a command,
in the worst case the emergency stop, so an update of a vendor library is merged only after the
owner has tried it on the mower. Two workflows use this script, standard library only (the
runner's ``python3``), and compare the base and head ``uv.lock``:

* ``vendor-lib-label.yml`` (``pull_request_target``, opened/reopened) runs ``changes`` with the
  base branch's copy of this script and labels a pull request that updates a vendor library
  ``vendor-lib`` and ``needs-hardware-test``;
* ``vendor-lib-guard.yml`` (``pull_request``) runs ``check``: the check fails while such a pull
  request carries ``needs-hardware-test`` (or has no ``vendor-lib`` label yet) and passes once
  the owner removed ``needs-hardware-test`` after the hardware test. Every other pull request
  passes at once.

Only an update counts: a vendor library in both lock files with a different version. Adding
a new adapter (and its library) is not an update; that pull request is reviewed on its own.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

VENDOR_LIBS = ("pymammotion", "pyworxcloud", "aioautomower")
"""The vendor libraries of the adapters (``[tool.mowdeck.adapter] vendor-modules``)."""

LABEL_VENDOR = "vendor-lib"
"""Marks a pull request that updates a vendor library."""
LABEL_NEEDS_TEST = "needs-hardware-test"
"""Blocks the merge until the owner removes it after the hardware test."""

CHECKLIST = "docs/ops/vendor-lib-updates.md"


@dataclass(frozen=True)
class Update:
    """A vendor library whose locked version differs between base and head."""

    name: str
    old: tuple[str, ...]
    new: tuple[str, ...]

    def describe(self) -> str:
        return f"{self.name} {', '.join(self.old)} -> {', '.join(self.new)}"


def _normalise(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def locked_versions(lock_text: str, names: Iterable[str] = VENDOR_LIBS) -> dict[str, set[str]]:
    """The locked versions of ``names`` in a ``uv.lock`` (a package can be locked twice, for
    different markers)."""
    wanted = {_normalise(n) for n in names}
    versions: dict[str, set[str]] = {}
    for package in tomllib.loads(lock_text).get("package", []):
        name = _normalise(str(package.get("name", "")))
        if name in wanted and "version" in package:
            versions.setdefault(name, set()).add(str(package["version"]))
    return versions


def vendor_updates(base_lock: str, head_lock: str) -> list[Update]:
    """Vendor libraries locked in both files with different versions."""
    base = locked_versions(base_lock)
    head = locked_versions(head_lock)
    return [
        Update(name, tuple(sorted(base[name])), tuple(sorted(head[name])))
        for name in sorted(base.keys() & head.keys())
        if base[name] != head[name]
    ]


def verdict(updates: Sequence[Update], labels: Iterable[str]) -> tuple[bool, str]:
    """Whether the check passes, with the summary that tells the owner what to do."""
    if not updates:
        return True, "No vendor library update in this pull request: nothing to test on a mower."
    present = set(labels)
    lines = "\n".join(f"- `{u.describe()}`" for u in updates)
    head = f"This pull request updates a vendor library:\n\n{lines}\n\n"
    if LABEL_NEEDS_TEST in present or LABEL_VENDOR not in present:
        return False, (
            f"{head}It is merged only after a test on real hardware: a bug in the library can "
            "break a command, in the worst case the emergency stop.\n\n"
            f"1. Run the checklist in `{CHECKLIST}` for every adapter listed above, safety "
            "commands first.\n"
            f"2. Passed: remove the label `{LABEL_NEEDS_TEST}` (keep `{LABEL_VENDOR}`); this "
            "check runs again and passes. Then approve and merge by hand (never auto-merged).\n"
            "3. Failed: leave the label, note what failed in the pull request body and close "
            "it; Dependabot then skips this release and proposes the next one.\n"
        )
    return True, (
        f"{head}The label `{LABEL_NEEDS_TEST}` was removed: the owner recorded the hardware "
        "test. Merge by hand."
    )


def _labels(raw: str) -> list[str]:
    value = json.loads(raw) if raw.strip() else []
    if not isinstance(value, list):
        msg = "--labels must be a JSON array of label names"
        raise SystemExit(msg)
    return [str(item) for item in value]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("changes", "check"):
        command = sub.add_parser(name)
        command.add_argument("--base", type=Path, required=True, help="base uv.lock")
        command.add_argument("--head", type=Path, required=True, help="head uv.lock")
        if name == "check":
            command.add_argument("--labels", default="[]", help="JSON array of label names")
    args = parser.parse_args(argv)

    updates = vendor_updates(
        args.base.read_text(encoding="utf-8"), args.head.read_text(encoding="utf-8")
    )
    if args.command == "changes":
        for update in updates:
            print(update.describe())
        return 0

    ok, summary = verdict(updates, _labels(args.labels))
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as out:
            out.write(f"## Vendor library hardware test\n\n{summary}\n")
    if not ok:
        print(f"::error title=Hardware test pending::Remove '{LABEL_NEEDS_TEST}' after the test")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
