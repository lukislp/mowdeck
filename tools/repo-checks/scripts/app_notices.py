# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Third-party notices of the native app bundle, and the licence policy that guards them.

The iOS and Android apps ship the bundled pages of ``apps/mobile`` (built from ``src/``), the
``@mowdeck/ui`` stylesheets with the IBM Plex webfonts, Capacitor and its plugins. Their licences
(MIT, SIL OFL 1.1, ...) require the licence text and the copyright notice to travel with the app,
and the server image gate (``licence_gate.py``) never sees the app. This script is the one
source of truth for that::

    python app_notices.py check
        Policy and lock file only, no ``node_modules``: the packages the lock file puts into the
        bundle (production dependencies of ``apps/mobile``, followed through workspace links
        such as ``@mowdeck/ui``, minus the policy's ``exclude`` list) are exactly the packages of
        the committed notices file, every licence is allowed, nothing is copyleft, the notices
        file is well formed and the bundled pages import none of the excluded packages. Needs no
        installed packages, so it runs in the CI job ``ci-config`` on every pull request.

    python app_notices.py check --installed
        Additionally reads the licence, copyright and NOTICE files of the installed packages and
        fails when the committed notices file differs: after ``pnpm install`` (CI job ``web``).

    python app_notices.py generate
        Writes the notices file from the installed packages.

The committed file (``apps/mobile/public/third-party-notices.json``, bundled by Vite, shown by
the About page) names a package by name, licence and copyright holder, never by version, and the
comparison of ``--installed`` ignores the years of a notice, so a version bump of a package changes
nothing here. A new package, a changed licence or a changed holder does, as it should. The policy
(``apps/mobile/third-party-policy.toml``) lists the allowed SPDX identifiers, the packages that
are in the dependency list but not in the bundle, and the overrides for a package whose metadata
lacks a licence or a holder; every entry carries a reason and a stale entry fails the gate.

Standard library only (Python 3.11 or newer), like ``ios_release_check.py``, which checks that
the built bundle carries the committed file.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
MOBILE = REPO / "apps" / "mobile"
POLICY = MOBILE / "third-party-policy.toml"
NOTICES = MOBILE / "public" / "third-party-notices.json"
LOCKFILE = REPO / "pnpm-lock.yaml"
ROOT_IMPORTER = "apps/mobile"
SCHEMA = 1

# Licences of code that must not ship inside the app (the app's own AGPL code is not a
# dependency). A hit fails the gate whatever the policy allows.
COPYLEFT = re.compile(r"(^|[^A-Za-z])(A|L)?GPL|EUPL|CPAL|OSL|SSPL|CC-BY-SA|MPL|EPL|CDDL", re.I)

# What the licence text of a package has to contain, so a package.json that says "MIT" over a
# different text is noticed. Whitespace and case do not matter.
PHRASES = {
    "MIT": "permission is hereby granted, free of charge",
    "0BSD": "permission to use, copy, modify, and/or distribute this software for any purpose",
    "ISC": "permission to use, copy, modify, and/or distribute this software for any purpose",
    "OFL-1.1": "sil open font license version 1.1",
    "Apache-2.0": "apache license version 2.0",
    "BSD-2-Clause": "redistribution and use in source and binary forms",
    "BSD-3-Clause": "neither the name of",
}

# "Copyright Holder(s)" is a phrase of the OFL, not a statement; the capital C keeps the
# upper-case disclaimer ("... COPYRIGHT HOLDER BE LIABLE ...") out as well.
_STATEMENT = re.compile(r"^\s*(?:Copyright\b(?!\s+Holder)|\(c\)|©)")
_SPLIT = re.compile(r"(?=Copyright\b(?!\s+Holder))")
_FILE_LABEL = re.compile(r"\s*\S+\.(?:ttf|otf|woff2?)\s*:\s*$", re.I)
_YEARS = re.compile(
    r"^\d{4}(?:\s*[-\u2013]\s*(?:\d{4}|present))?(?:\s*,\s*\d{4}(?:\s*[-\u2013]\s*(?:\d{4}|present))?)*",
    re.I,
)
_URL_LINE = re.compile(r"^\s*https?://\S+\s*$")
_LICENCE_FILE = re.compile(r"^(licen[cs]e|copying)(?:[-_.].*)?$", re.I)
_NOTICE_FILE = re.compile(r"^notice(?:[-_.].*)?$", re.I)
_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)['"]([^'"]+)['"]""")


class CheckError(Exception):
    """One or more problems, each reported as a GitHub error annotation."""


# --- policy -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """``third-party-policy.toml``."""

    allowed: frozenset[str]
    exclude: dict[str, str]
    override: dict[str, dict[str, str]]


def load_policy(path: Path = POLICY) -> Policy:
    """Read and validate the policy file."""
    data = tomllib.loads(path.read_text("utf-8"))
    problems = []
    allowed = frozenset(data.get("allowed", []))
    if not allowed:
        problems.append("policy: allowed is empty")
    for licence in sorted(allowed):
        if COPYLEFT.search(licence):
            problems.append(f"policy: {licence} is copyleft and can never be allowed in the app")
    exclude: dict[str, str] = {}
    for entry in data.get("exclude", []):
        if not entry.get("reason"):
            problems.append(f"policy: exclude {entry.get('package')!r} needs a reason")
        exclude[str(entry.get("package"))] = str(entry.get("reason"))
    override: dict[str, dict[str, str]] = {}
    for entry in data.get("override", []):
        name = str(entry.get("package"))
        if not entry.get("reason"):
            problems.append(f"policy: override {name!r} needs a reason")
        if not entry.get("licence") and not entry.get("holder"):
            problems.append(f"policy: override {name!r} sets neither licence nor holder")
        if entry.get("licence") and COPYLEFT.search(str(entry["licence"])):
            problems.append(f"policy: override {name!r} names a copyleft licence")
        override[name] = {k: str(v) for k, v in entry.items()}
    if problems:
        raise CheckError("\n".join(problems))
    return Policy(allowed, exclude, override)


# --- the lock file ------------------------------------------------------------------------------


_KEY = re.compile(r"^(?:'((?:[^']|'')*)'|([^'\s].*?)):(?:\s+(.*))?$")


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


def parse_lock(text: str) -> dict[str, Any]:
    """The ``importers`` and ``snapshots`` sections of a pnpm lock file (version 9) as nested
    dicts. A restricted indentation parser: the format is regular, and the script must run on a
    bare ``python3`` without PyYAML. Anything else than version 9 is refused."""
    if not re.search(r"^lockfileVersion: '9\.0'$", text, re.M):
        msg = "pnpm-lock.yaml is not lockfileVersion 9.0: update app_notices.parse_lock"
        raise CheckError(msg)
    root: dict[str, Any] = {"importers": {}, "snapshots": {}}
    stack: list[tuple[int, dict[str, Any]]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            name = raw.split(":", 1)[0]
            stack = [(0, root[name])] if name in root else []
            continue
        if not stack or raw.strip().startswith("- "):
            continue
        match = _KEY.match(raw.strip())
        if not match:
            msg = f"pnpm-lock.yaml: cannot read line {raw!r}"
            raise CheckError(msg)
        key = match.group(1).replace("''", "'") if match.group(1) is not None else match.group(2)
        value = match.group(3)
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if value is None:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        elif value == "{}":
            parent[key] = {}
        else:
            parent[key] = _unquote(value)
    return root


def _snapshot_name(key: str) -> str:
    return key.split("(", 1)[0].rsplit("@", 1)[0]


def bundle_packages(
    lock: dict[str, Any], exclude: set[str], root: str = ROOT_IMPORTER
) -> dict[str, set[str]]:
    """Package name -> versions of every third-party package the lock file puts into the app:
    the production dependencies of ``root``, those of the workspace packages it links to, and
    their dependencies, minus the excluded names (and what only they pull in)."""
    importers = lock.get("importers", {})
    snapshots = lock.get("snapshots", {})
    found: dict[str, set[str]] = {}
    queue: list[str] = []
    seen_importers: set[str] = set()

    def add(name: str, version: str) -> None:
        if name in exclude or version.startswith("link:"):
            return
        key = f"{name}@{version}"
        if key not in queue:
            queue.append(key)

    def walk_importer(path: str) -> None:
        if path in seen_importers:
            return
        seen_importers.add(path)
        entry = importers.get(path)
        if entry is None:
            msg = f"pnpm-lock.yaml has no importer {path}"
            raise CheckError(msg)
        for kind in ("dependencies", "optionalDependencies"):
            for name, dep in entry.get(kind, {}).items():
                version = dep["version"]
                if version.startswith("link:"):
                    walk_importer(posixpath.normpath(posixpath.join(path, version[5:])))
                else:
                    add(name, version)

    walk_importer(root)
    index = 0
    while index < len(queue):
        key = queue[index]
        index += 1
        name = _snapshot_name(key)
        found.setdefault(name, set()).add(key[len(name) + 1 :])
        snapshot = snapshots.get(key)
        if snapshot is None:
            msg = f"pnpm-lock.yaml has no snapshot {key}"
            raise CheckError(msg)
        for kind in ("dependencies", "optionalDependencies"):
            for dep, version in snapshot.get(kind, {}).items():
                add(dep, version)
    return found


# --- licence and copyright text ------------------------------------------------------------------


def _squash(text: str) -> str:
    return " ".join(text.lower().split())


def copyright_lines(text: str) -> list[str]:
    """The copyright statements of a licence file, each once, in order. A file that lists one
    statement per font file ("Copyright 2017 IBM Corp. ... Font-Thin.ttf: Copyright ...") yields
    the distinct statements without the file labels."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if not _STATEMENT.match(raw):
            continue
        for chunk in _SPLIT.split(raw.strip()):
            statement = _FILE_LABEL.sub("", chunk).strip()
            if statement and statement not in lines:
                lines.append(statement)
    return lines


def holder(statement: str) -> str:
    """The copyright holder of a statement (``Copyright (c) 2017-present Drifty Co.`` ->
    ``Drifty Co``); empty when none can be read."""
    rest = re.sub(r"^\s*(?:copyright)?\s*(?:\(c\)|©)?\s*", "", statement, flags=re.I)
    rest = _YEARS.sub("", rest).lstrip(" ,")
    rest = re.sub(r"\s*All rights reserved\.?\s*$", "", rest, flags=re.I)
    return rest.strip(" .,")


def licence_body(text: str) -> str:
    """The licence text without its copyright statements (those are listed per package)."""
    kept: list[str] = []
    dropped_before = False
    for raw in text.replace("\r\n", "\n").split("\n"):
        if _STATEMENT.match(raw):
            dropped_before = True
            continue
        if dropped_before and _URL_LINE.match(raw):
            dropped_before = False
            continue
        dropped_before = False
        kept.append(raw.rstrip())
    body = "\n".join(kept).strip()
    return re.sub(r"\n{3,}", "\n\n", body)


def licence_ok(expression: str, allowed: frozenset[str]) -> bool:
    """Whether an SPDX expression is allowed: ``OR`` needs one allowed side, ``AND`` all."""
    expression = expression.strip()
    while expression.startswith("(") and expression.endswith(")"):
        expression = expression[1:-1].strip()
    alternatives = re.split(r"\s+OR\s+", expression, flags=re.I)
    if len(alternatives) > 1:
        return any(licence_ok(part, allowed) for part in alternatives)
    parts = re.split(r"\s+AND\s+", expression, flags=re.I)
    if len(parts) > 1:
        return all(licence_ok(part, allowed) for part in parts)
    return expression in allowed and not COPYLEFT.search(expression)


# --- installed packages -------------------------------------------------------------------------


@dataclass
class Notice:
    """What the app has to reproduce for one package."""

    name: str
    licence: str
    copyright: list[str]
    text: str
    notice: str | None = None
    problems: list[str] = field(default_factory=list)


def _declared_licence(meta: dict[str, Any]) -> str | None:
    value = meta.get("license")
    if isinstance(value, dict):
        value = value.get("type")
    if not value and isinstance(meta.get("licenses"), list) and meta["licenses"]:
        value = " OR ".join(str(item.get("type")) for item in meta["licenses"])
    return str(value).strip() if value else None


def locate(name: str, versions: set[str], repo: Path = REPO) -> Path | None:
    """The installed directory of a package (pnpm layout)."""
    for base in (MOBILE, repo / "libs" / "mowdeck-ui"):
        direct = base / "node_modules" / Path(*name.split("/"))
        if (direct / "package.json").is_file():
            return direct
    pnpm = repo / "node_modules" / ".pnpm"
    for version in sorted({v.split("(", 1)[0] for v in versions}):
        folder = f"{name.replace('/', '+')}@{version}"
        for candidate in sorted(pnpm.glob(f"{glob_escape(folder)}*")):
            target = candidate / "node_modules" / Path(*name.split("/"))
            if (target / "package.json").is_file():
                return target
    return None


def glob_escape(text: str) -> str:
    """Escape the glob metacharacters of a folder name."""
    return re.sub(r"([\[\]*?])", r"[\1]", text)


def read_notice(name: str, directory: Path, policy: Policy) -> Notice:
    """Licence, copyright statements, licence text and NOTICE of an installed package."""
    meta = json.loads((directory / "package.json").read_text("utf-8"))
    files = sorted(p for p in directory.iterdir() if p.is_file())
    licence_file = next((p for p in files if _LICENCE_FILE.match(p.name)), None)
    notice_file = next((p for p in files if _NOTICE_FILE.match(p.name)), None)
    text = licence_file.read_text("utf-8", errors="replace") if licence_file else ""
    override = policy.override.get(name, {})
    licence = override.get("licence") or _declared_licence(meta) or ""
    statements = copyright_lines(text)
    result = Notice(
        name=name,
        licence=licence,
        copyright=statements,
        text=licence_body(text),
        notice=(notice_file.read_text("utf-8", errors="replace").strip() if notice_file else None),
    )
    if not licence:
        result.problems.append(f"{name}: no licence in its package.json (name it in the policy)")
    elif COPYLEFT.search(licence):
        result.problems.append(f"{name}: {licence} is copyleft and must not ship in the app")
    elif not licence_ok(licence, policy.allowed):
        result.problems.append(f"{name}: licence {licence} is not allowed by the policy")
    elif licence_file is None:
        result.problems.append(f"{name}: no licence file, the licence text cannot be reproduced")
    elif licence in PHRASES and PHRASES[licence] not in _squash(text):
        result.problems.append(f"{name}: package.json says {licence}, its licence file does not")
    holders = [holder(s) for s in statements if holder(s)]
    if override.get("holder"):
        if not statements:
            result.copyright = [f"Copyright {override['holder']}"]
    elif not holders:
        result.problems.append(f"{name}: no copyright holder found in its licence file")
    return result


# --- the notices file ---------------------------------------------------------------------------


def build_document(notices: list[Notice]) -> dict[str, Any]:
    """The notices file: one block per licence (text once), the packages under it."""
    by_licence: dict[str, list[Notice]] = {}
    for item in sorted(notices, key=lambda n: n.name):
        by_licence.setdefault(item.licence, []).append(item)
    licences = []
    for licence in sorted(by_licence):
        packages = []
        for item in by_licence[licence]:
            entry: dict[str, Any] = {"name": item.name, "copyright": item.copyright}
            if item.notice:
                entry["notice"] = item.notice
            packages.append(entry)
        licences.append({"id": licence, "text": by_licence[licence][0].text, "packages": packages})
    return {
        "schema": SCHEMA,
        "generator": "tools/repo-checks/scripts/app_notices.py (do not edit by hand)",
        "licences": licences,
    }


def render(document: dict[str, Any]) -> str:
    """The file's bytes: stable, readable JSON."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def collect(policy: Policy, repo: Path = REPO) -> list[Notice]:
    """Read every bundled package from ``node_modules``; raises with all problems."""
    lock = parse_lock((repo / "pnpm-lock.yaml").read_text("utf-8"))
    packages = bundle_packages(lock, set(policy.exclude))
    notices: list[Notice] = []
    problems: list[str] = []
    for name in sorted(packages):
        directory = locate(name, packages[name], repo)
        if directory is None:
            problems.append(f"{name}: not installed (run pnpm install)")
            continue
        notice = read_notice(name, directory, policy)
        problems += notice.problems
        notices.append(notice)
    if problems:
        raise CheckError("\n".join(problems))
    return notices


def identity(document: dict[str, Any]) -> dict[str, Any]:
    """What makes two notices files the same: licences with their text, and per package the
    holders (not the years) and the NOTICE."""
    return {
        entry["id"]: {
            "text": entry["text"],
            "packages": {
                pkg["name"]: {
                    "holders": sorted({holder(s) for s in pkg["copyright"]}),
                    "notice": pkg.get("notice"),
                }
                for pkg in entry["packages"]
            },
        }
        for entry in document["licences"]
    }


def check_document(
    document: Any, expected: dict[str, set[str]], policy: Policy, label: str = "notices file"
) -> list[str]:
    """Policy and lock file against the (committed) notices file, without node_modules."""
    problems: list[str] = []
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return [f"{label}: not a schema {SCHEMA} notices file"]
    listed: dict[str, str] = {}
    for entry in document.get("licences", []):
        licence = str(entry.get("id"))
        if COPYLEFT.search(licence):
            problems.append(f"{label}: {licence} is copyleft and must not ship in the app")
        elif not licence_ok(licence, policy.allowed):
            problems.append(f"{label}: licence {licence} is not allowed by the policy")
        if not str(entry.get("text", "")).strip():
            problems.append(f"{label}: no licence text for {licence}")
        for pkg in entry.get("packages", []):
            name = str(pkg.get("name"))
            if name in listed:
                problems.append(f"{label}: {name} is listed twice")
            listed[name] = licence
            statements = pkg.get("copyright") or []
            if not any(holder(str(s)) for s in statements):
                problems.append(f"{label}: {name} has no copyright holder")
    for name in sorted(set(expected) - set(listed)):
        problems.append(
            f"{label}: {name} is in the app bundle but not in the notices file "
            "(run app_notices.py generate and commit the result)"
        )
    for name in sorted(set(listed) - set(expected)):
        problems.append(
            f"{label}: {name} is no longer in the app bundle "
            "(run app_notices.py generate and commit the result)"
        )
    return problems


def check_policy_use(
    policy: Policy, lock: dict[str, Any], used_overrides: set[str] | None = None
) -> list[str]:
    """Every exclude and override of the policy still applies (no stale entries)."""
    problems = []
    every = {_snapshot_name(key) for key in lock.get("snapshots", {})}
    for name in sorted(policy.exclude):
        if name not in every:
            problems.append(f"policy: exclude {name!r} is not in the lock file any more, drop it")
    for name in sorted(policy.override):
        if name not in every:
            problems.append(f"policy: override {name!r} is not in the lock file any more, drop it")
        elif used_overrides is not None and name not in used_overrides:
            problems.append(f"policy: override {name!r} is not needed any more, drop it")
    return problems


def check_sources(src: Path, policy: Policy) -> list[str]:
    """The bundled pages must not import what the policy excludes (they would then ship it):
    the barrel ``@mowdeck/ui`` pulls in Leaflet; the stylesheets are imported by sub-path."""
    problems = []
    for path in sorted(src.rglob("*.ts")):
        if path.name.endswith(".test.ts"):
            continue
        for specifier in _IMPORT.findall(path.read_text("utf-8")):
            package = "/".join(specifier.split("/")[: 2 if specifier.startswith("@") else 1])
            if specifier == "@mowdeck/ui" or package in policy.exclude:
                problems.append(
                    f"{path.relative_to(src.parent).as_posix()} imports {specifier}, which the "
                    "policy excludes from the app bundle: its notice would be missing"
                )
    return problems


def check(installed: bool = False, repo: Path = REPO) -> None:
    """Every check of the module; raises CheckError with all problems."""
    mobile = repo / "apps" / "mobile"
    policy = load_policy(mobile / "third-party-policy.toml")
    lock = parse_lock((repo / "pnpm-lock.yaml").read_text("utf-8"))
    expected = bundle_packages(lock, set(policy.exclude))
    notices_path = mobile / "public" / "third-party-notices.json"
    if not notices_path.is_file():
        msg = (
            f"{notices_path.relative_to(repo).as_posix()} is missing (run app_notices.py generate)"
        )
        raise CheckError(msg)
    document = json.loads(notices_path.read_text("utf-8"))
    problems = check_document(document, expected, policy)
    problems += check_policy_use(policy, lock)
    problems += check_sources(mobile / "src", policy)
    if installed and not problems:
        fresh = build_document(collect(policy, repo))
        if identity(fresh) != identity(document):
            problems.append(
                "third-party notices file is out of date with the installed packages "
                "(licence, holder or NOTICE changed): run app_notices.py generate and commit it"
            )
        needed = {n for n in policy.override if n in expected}
        problems += check_policy_use(policy, lock, needed)
    if problems:
        raise CheckError("\n".join(problems))


def generate(repo: Path = REPO) -> dict[str, Any]:
    """Write the notices file from the installed packages."""
    mobile = repo / "apps" / "mobile"
    policy = load_policy(mobile / "third-party-policy.toml")
    document = build_document(collect(policy, repo))
    target = mobile / "public" / "third-party-notices.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(render(document), "utf-8", newline="\n")
    return document


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Third-party notices of the app bundle")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--installed", action="store_true")
    sub.add_parser("generate")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            check(installed=args.installed)
            print("App bundle notices: packages, licences and policy are in order.")
        else:
            document = generate()
            count = sum(len(entry["packages"]) for entry in document["licences"])
            print(f"Wrote {NOTICES.relative_to(REPO).as_posix()}: {count} packages.")
    except CheckError as exc:
        for line in str(exc).splitlines():
            print(f"::error::{line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
