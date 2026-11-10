# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Tests of app_notices.py: the third-party notices and licence policy of the app bundle.

The pure parts (lock file, licence text, policy) run on fixtures; the repository tests check the
committed notices file and policy against the real lock file, which is what CI runs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture(scope="module")
def notices(repo_root: Path) -> ModuleType:
    """app_notices.py as a module."""
    path = repo_root / "tools" / "repo-checks" / "scripts" / "app_notices.py"
    spec = importlib.util.spec_from_file_location("app_notices", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LOCK = """\
lockfileVersion: '9.0'

settings:
  autoInstallPeers: false

importers:

  .:
    devDependencies:
      eslint:
        specifier: 1.0.0
        version: 1.0.0

  apps/mobile:
    dependencies:
      '@cap/core':
        specifier: 1.0.0
        version: 1.0.0
      '@cap/plugin':
        specifier: 2.0.0
        version: 2.0.0(@cap/core@1.0.0)
      '@mowdeck/ui':
        specifier: workspace:*
        version: link:../../libs/mowdeck-ui
    devDependencies:
      vite:
        specifier: 8.0.0
        version: 8.0.0

  libs/mowdeck-ui:
    dependencies:
      font-pkg:
        specifier: 3.0.0
        version: 3.0.0
      leaflet:
        specifier: 1.9.4
        version: 1.9.4
    devDependencies:
      vitest:
        specifier: 5.0.0
        version: 5.0.0

packages:

  '@cap/core@1.0.0':
    resolution: {integrity: sha512-x}

snapshots:

  '@cap/core@1.0.0':
    dependencies:
      tslib: 2.8.1

  '@cap/plugin@2.0.0(@cap/core@1.0.0)':
    dependencies:
      '@cap/core': 1.0.0

  font-pkg@3.0.0: {}

  leaflet@1.9.4: {}

  tslib@2.8.1: {}

  vite@8.0.0:
    dependencies:
      rollup: 4.0.0
    transitivePeerDependencies:
      - debug

  rollup@4.0.0: {}
"""

MIT = """MIT License

Copyright (c) 2017-present Drifty Co.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software, to deal in the Software without restriction.
"""

IONIC = """Copyright 2020-present Ionic
https://ionic.io

MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software, to deal in the Software without restriction.
"""

OFL = """Copyright 2017 IBM Corp. All rights reserved. Font-Thin.ttf: Copyright 2017 IBM Corp. \
All rights reserved. Font-Bold.ttf: Copyright 2017 IBM Corp. All rights reserved.

This Font Software is licensed under the SIL Open Font License, Version 1.1.

-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------
Copyright Holder(s) and the Author(s) or with their explicit written
permission. THE COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM.
"""


def _policy(notices: ModuleType, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "allowed": frozenset({"MIT", "OFL-1.1", "0BSD"}),
        "exclude": {"leaflet": "not imported"},
        "override": {},
    }
    values.update(overrides)
    return notices.Policy(**values)


# --- the lock file -----------------------------------------------------------------------------


def test_lock_file_sections_are_parsed(notices: ModuleType) -> None:
    lock = notices.parse_lock(LOCK)
    assert set(lock["importers"]) == {".", "apps/mobile", "libs/mowdeck-ui"}
    mobile = lock["importers"]["apps/mobile"]["dependencies"]
    assert mobile["@mowdeck/ui"]["version"] == "link:../../libs/mowdeck-ui"
    assert "@cap/plugin@2.0.0(@cap/core@1.0.0)" in lock["snapshots"]
    assert lock["snapshots"]["vite@8.0.0"]["dependencies"] == {"rollup": "4.0.0"}
    assert lock["snapshots"]["leaflet@1.9.4"] == {}


def test_only_lock_file_version_9_is_read(notices: ModuleType) -> None:
    with pytest.raises(notices.CheckError, match=r"lockfileVersion 9\.0"):
        notices.parse_lock(LOCK.replace("'9.0'", "'10.0'"))


def test_bundle_follows_workspace_links_and_skips_dev_and_excluded(notices: ModuleType) -> None:
    found = notices.bundle_packages(notices.parse_lock(LOCK), {"leaflet"})
    assert sorted(found) == ["@cap/core", "@cap/plugin", "font-pkg", "tslib"]
    assert found["@cap/plugin"] == {"2.0.0(@cap/core@1.0.0)"}
    # without the exclusion Leaflet would be in the bundle
    assert "leaflet" in notices.bundle_packages(notices.parse_lock(LOCK), set())


def test_bundle_needs_every_snapshot(notices: ModuleType) -> None:
    lock = notices.parse_lock(LOCK.replace("  tslib@2.8.1: {}\n", ""))
    with pytest.raises(notices.CheckError, match=r"no snapshot tslib@2\.8\.1"):
        notices.bundle_packages(lock, set())


def test_repository_lock_file_is_readable_and_bundle_is_the_known_set(
    notices: ModuleType, repo_root: Path
) -> None:
    lock = notices.parse_lock((repo_root / "pnpm-lock.yaml").read_text("utf-8"))
    found = notices.bundle_packages(lock, {"leaflet"})
    assert {"@capacitor/core", "@capacitor/ios", "@fontsource/ibm-plex-mono"} <= set(found)
    assert not {"vite", "vitest", "typescript", "leaflet", "svelte"} & set(found)


# --- licence text, copyright and holder ----------------------------------------------------------


def test_copyright_statements_are_found_once(notices: ModuleType) -> None:
    assert notices.copyright_lines(MIT) == ["Copyright (c) 2017-present Drifty Co."]
    assert notices.copyright_lines(IONIC) == ["Copyright 2020-present Ionic"]
    # a font licence repeats the statement per font file; the OFL's own words are no statement
    assert notices.copyright_lines(OFL) == ["Copyright 2017 IBM Corp. All rights reserved."]


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("Copyright (c) 2017-present Drifty Co.", "Drifty Co"),
        ("Copyright 2020-present Ionic", "Ionic"),
        ("Copyright (c) Microsoft Corporation.", "Microsoft Corporation"),
        ("Copyright 2017 IBM Corp. All rights reserved.", "IBM Corp"),
        ("Copyright (c) 2014, 2016-2020 Jane Doe", "Jane Doe"),
        ("Copyright (c) 2021 Robin Genz", "Robin Genz"),
        ("Copyright 2020", ""),
        ("Copyright (c)", ""),
    ],
)
def test_holder(notices: ModuleType, statement: str, expected: str) -> None:
    assert notices.holder(statement) == expected


def test_licence_body_drops_statements_but_keeps_the_licence(notices: ModuleType) -> None:
    body = notices.licence_body(IONIC)
    assert "Copyright 2020" not in body
    assert "ionic.io" not in body
    assert body.startswith("MIT License")
    assert "Permission is hereby granted" in body
    ofl = notices.licence_body(OFL)
    assert "IBM" not in ofl
    assert "Copyright Holder(s)" in ofl
    assert "COPYRIGHT HOLDER BE LIABLE" in ofl


@pytest.mark.parametrize(
    ("expression", "ok"),
    [
        ("MIT", True),
        ("(MIT OR GPL-3.0-only)", True),
        ("MIT AND OFL-1.1", True),
        ("MIT AND Apache-2.0", False),
        ("GPL-3.0-only", False),
        ("UNLICENSED", False),
        ("SEE LICENSE IN LICENSE.txt", False),
    ],
)
def test_licence_expression(notices: ModuleType, expression: str, ok: bool) -> None:
    assert notices.licence_ok(expression, frozenset({"MIT", "OFL-1.1"})) is ok


# --- the policy ----------------------------------------------------------------------------------


def _write_policy(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policy.toml"
    path.write_text(text, "utf-8")
    return path


def test_repository_policy_loads(notices: ModuleType) -> None:
    policy = notices.load_policy()
    assert "MIT" in policy.allowed
    assert "leaflet" in policy.exclude


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("allowed = []\n", "allowed is empty"),
        ('allowed = ["MIT", "AGPL-3.0-only"]\n', "copyleft"),
        ('allowed = ["MIT", "LGPL-2.1"]\n', "copyleft"),
        ('allowed = ["MIT"]\n[[exclude]]\npackage = "x"\n', "needs a reason"),
        ('allowed = ["MIT"]\n[[override]]\npackage = "x"\nlicence = "MIT"\n', "needs a reason"),
        (
            'allowed = ["MIT"]\n[[override]]\npackage = "x"\nreason = "r"\n',
            "neither licence nor holder",
        ),
        (
            'allowed = ["MIT"]\n[[override]]\npackage = "x"\n'
            'licence = "GPL-3.0-only"\nreason = "r"\n',
            "copyleft",
        ),
    ],
)
def test_bad_policies_are_refused(
    notices: ModuleType, tmp_path: Path, text: str, fragment: str
) -> None:
    with pytest.raises(notices.CheckError, match=fragment):
        notices.load_policy(_write_policy(tmp_path, text))


def test_stale_policy_entries_fail(notices: ModuleType) -> None:
    lock = notices.parse_lock(LOCK)
    policy = _policy(
        notices,
        exclude={"leaflet": "r", "gone": "r"},
        override={"font-pkg": {"holder": "x"}, "vanished": {"holder": "y"}},
    )
    problems = notices.check_policy_use(policy, lock, used_overrides=set())
    assert any("exclude 'gone'" in p for p in problems)
    assert any("override 'vanished'" in p and "not in the lock file" in p for p in problems)
    assert any("override 'font-pkg'" in p and "not needed" in p for p in problems)
    assert not any("leaflet" in p for p in problems)


# --- installed packages --------------------------------------------------------------------------


def _package(root: Path, name: str, meta: dict[str, Any], files: dict[str, str]) -> Path:
    directory = root / Path(*name.split("/"))
    directory.mkdir(parents=True)
    (directory / "package.json").write_text(json.dumps({"name": name, **meta}), "utf-8")
    for filename, content in files.items():
        (directory / filename).write_text(content, "utf-8")
    return directory


def test_installed_package_notice(notices: ModuleType, tmp_path: Path) -> None:
    directory = _package(tmp_path, "@cap/core", {"license": "MIT"}, {"LICENSE": MIT})
    notice = notices.read_notice("@cap/core", directory, _policy(notices))
    assert notice.problems == []
    assert notice.licence == "MIT"
    assert notice.copyright == ["Copyright (c) 2017-present Drifty Co."]
    assert notice.notice is None


def test_apache_notice_file_is_included(notices: ModuleType, tmp_path: Path) -> None:
    text = "Apache License\nVersion 2.0, January 2004\nCopyright 2020 Acme Inc\n"
    directory = _package(
        tmp_path, "acme", {"license": "Apache-2.0"}, {"LICENSE": text, "NOTICE": "Acme\nby Bob\n"}
    )
    policy = _policy(notices, allowed=frozenset({"Apache-2.0"}))
    notice = notices.read_notice("acme", directory, policy)
    assert notice.problems == []
    assert notice.notice == "Acme\nby Bob"


@pytest.mark.parametrize(
    ("meta", "files", "fragment"),
    [
        ({}, {"LICENSE": MIT}, "no licence in its package.json"),
        ({"license": "GPL-3.0-only"}, {"LICENSE": MIT}, "copyleft"),
        ({"license": "LGPL-2.1-or-later"}, {"LICENSE": MIT}, "copyleft"),
        ({"license": "AGPL-3.0-only"}, {"LICENSE": MIT}, "copyleft"),
        ({"license": "BSD-3-Clause"}, {"LICENSE": MIT}, "not allowed by the policy"),
        ({"license": "SEE LICENSE IN x"}, {"LICENSE": MIT}, "not allowed by the policy"),
        ({"license": "MIT"}, {}, "no licence file"),
        ({"license": "MIT"}, {"LICENSE": "do what you want\nCopyright 2020 Bob"}, "does not"),
        (
            {"license": "MIT"},
            {"LICENSE": MIT.replace("Copyright (c) 2017-present Drifty Co.", "")},
            "no copyright holder",
        ),
        ({"license": "MIT"}, {"LICENSE": MIT.replace("Drifty Co.", "")}, "no copyright holder"),
    ],
)
def test_notice_problems(
    notices: ModuleType, tmp_path: Path, meta: dict[str, Any], files: dict[str, str], fragment: str
) -> None:
    directory = _package(tmp_path, "pkg", meta, files)
    notice = notices.read_notice("pkg", directory, _policy(notices))
    assert any(fragment in problem for problem in notice.problems), notice.problems


def test_policy_override_supplies_a_missing_licence_and_holder(
    notices: ModuleType, tmp_path: Path
) -> None:
    text = MIT.replace("Copyright (c) 2017-present Drifty Co.", "")
    directory = _package(tmp_path, "pkg", {}, {"LICENSE": text})
    policy = _policy(
        notices, override={"pkg": {"licence": "MIT", "holder": "Pat Doe", "reason": "r"}}
    )
    notice = notices.read_notice("pkg", directory, policy)
    assert notice.problems == []
    assert notice.licence == "MIT"
    assert notice.copyright == ["Copyright Pat Doe"]


def test_pnpm_layout_is_located(notices: ModuleType, tmp_path: Path) -> None:
    store = tmp_path / "node_modules" / ".pnpm" / "@cap+core@1.0.0_peer" / "node_modules"
    expected = _package(store, "@cap/core", {"license": "MIT"}, {})
    found = notices.locate("@cap/core", {"1.0.0(@cap/x@1.0.0)"}, repo=tmp_path)
    assert found == expected
    assert notices.locate("missing", {"1.0.0"}, repo=tmp_path) is None


# --- the notices file ----------------------------------------------------------------------------


def _notice(notices: ModuleType, name: str, licence: str, lines: list[str], text: str) -> Any:
    return notices.Notice(name=name, licence=licence, copyright=lines, text=text)


def test_document_groups_by_licence_and_names_no_version(notices: ModuleType) -> None:
    items = [
        _notice(notices, "b", "MIT", ["Copyright (c) 2020 B"], "MIT text"),
        _notice(notices, "a", "MIT", ["Copyright (c) 2019 A"], "MIT text"),
        _notice(notices, "f", "OFL-1.1", ["Copyright 2017 F"], "OFL text"),
    ]
    document = notices.build_document(items)
    assert [entry["id"] for entry in document["licences"]] == ["MIT", "OFL-1.1"]
    assert [p["name"] for p in document["licences"][0]["packages"]] == ["a", "b"]
    assert document["licences"][0]["text"] == "MIT text"
    assert "version" not in notices.render(document).lower()
    assert notices.render(document).endswith("}\n")


def test_identity_ignores_years_but_not_holders_or_notices(notices: ModuleType) -> None:
    def document(line: str, notice: str | None = None) -> dict[str, Any]:
        item = _notice(notices, "a", "MIT", [line], "t")
        item.notice = notice
        built: dict[str, Any] = notices.build_document([item])
        return built

    base = notices.identity(document("Copyright (c) 2020 Acme"))
    assert notices.identity(document("Copyright (c) 2020-2026 Acme")) == base
    assert notices.identity(document("Copyright (c) 2020 Other")) != base
    assert notices.identity(document("Copyright (c) 2020 Acme", "NOTICE")) != base


def _document(notices: ModuleType) -> dict[str, Any]:
    built: dict[str, Any] = notices.build_document(
        [
            _notice(notices, "a", "MIT", ["Copyright (c) 2020 A"], "MIT text"),
            _notice(notices, "b", "OFL-1.1", ["Copyright 2017 B"], "OFL text"),
        ]
    )
    return built


def test_matching_document_has_no_problems(notices: ModuleType) -> None:
    expected: dict[str, set[str]] = {"a": set(), "b": set()}
    assert notices.check_document(_document(notices), expected, _policy(notices)) == []


def test_new_and_dropped_packages_are_reported(notices: ModuleType) -> None:
    problems = notices.check_document(
        _document(notices), {"a": set(), "c": set()}, _policy(notices)
    )
    assert any("c is in the app bundle but not in the notices file" in p for p in problems)
    assert any("b is no longer in the app bundle" in p for p in problems)


def test_document_problems(notices: ModuleType) -> None:
    document = _document(notices)
    document["licences"][0]["id"] = "GPL-3.0-only"
    document["licences"][1]["text"] = " "
    document["licences"][1]["packages"][0]["copyright"] = ["Copyright 2017"]
    problems = notices.check_document(document, {"a": set(), "b": set()}, _policy(notices))
    assert any("GPL-3.0-only is copyleft" in p for p in problems)
    assert any("no licence text for OFL-1.1" in p for p in problems)
    assert any("b has no copyright holder" in p for p in problems)
    assert notices.check_document({"schema": 2}, {}, _policy(notices)) == [
        "notices file: not a schema 1 notices file"
    ]


def test_an_unallowed_licence_in_the_file_fails(notices: ModuleType) -> None:
    document = _document(notices)
    document["licences"][0]["id"] = "BSD-3-Clause"
    problems = notices.check_document(document, {"a": set(), "b": set()}, _policy(notices))
    assert any("BSD-3-Clause is not allowed" in p for p in problems)


# --- the bundled pages' imports -----------------------------------------------------------------


def test_pages_must_not_import_excluded_packages(notices: ModuleType, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.ts").write_text(
        "import '@mowdeck/ui/styles/base.css';\nimport { App } from '@capacitor/app';\n", "utf-8"
    )
    (src / "ok.test.ts").write_text("import leaflet from 'leaflet';\n", "utf-8")
    assert notices.check_sources(src, _policy(notices)) == []
    (src / "map.ts").write_text("import L from 'leaflet';\n", "utf-8")
    (src / "barrel.ts").write_text("export { Button } from '@mowdeck/ui';\n", "utf-8")
    (src / "deep.ts").write_text("const x = await import('leaflet/dist/x.js');\n", "utf-8")
    problems = notices.check_sources(src, _policy(notices))
    assert len(problems) == 3
    assert all("excludes" in p for p in problems)


def test_repository_pages_import_none_of_the_excluded_packages(
    notices: ModuleType, repo_root: Path
) -> None:
    assert notices.check_sources(repo_root / "apps" / "mobile" / "src", notices.load_policy()) == []


# --- the repository ------------------------------------------------------------------------------


def test_committed_notices_match_policy_and_lock_file(notices: ModuleType) -> None:
    """What CI runs without node_modules; a new dependency fails here until the file is
    regenerated, a version bump never does."""
    notices.check(installed=False)


def test_committed_notices_name_every_licence_once_with_its_text(
    notices: ModuleType, repo_root: Path
) -> None:
    document = json.loads(notices.NOTICES.read_text("utf-8"))
    ids = [entry["id"] for entry in document["licences"]]
    assert len(ids) == len(set(ids))
    assert {"MIT", "OFL-1.1"} <= set(ids)
    for entry in document["licences"]:
        assert notices.PHRASES[entry["id"]] in " ".join(entry["text"].lower().split())
    # the OFL text is required to travel with the fonts
    ofl = next(e for e in document["licences"] if e["id"] == "OFL-1.1")
    assert {p["name"] for p in ofl["packages"]} == {
        "@fontsource-variable/ibm-plex-sans",
        "@fontsource/ibm-plex-mono",
    }
    assert not any(notices.COPYLEFT.search(entry["id"]) for entry in document["licences"]), (
        "copyleft in the app"
    )
    # version-independent: no package version anywhere in the file
    assert '"version"' not in notices.NOTICES.read_text("utf-8")


def test_committed_notices_are_current_when_packages_are_installed(
    notices: ModuleType, repo_root: Path
) -> None:
    if not (repo_root / "apps" / "mobile" / "node_modules" / "@capacitor" / "core").exists():
        pytest.skip("run pnpm install for the installed-packages comparison")
    notices.check(installed=True)


def test_check_reports_a_stale_file(notices: ModuleType, tmp_path: Path, repo_root: Path) -> None:
    """A repository copy whose notices file lacks a package fails with the way out."""
    mobile = tmp_path / "apps" / "mobile"
    (mobile / "public").mkdir(parents=True)
    (mobile / "src").mkdir()
    (mobile / "third-party-policy.toml").write_text(
        (repo_root / "apps" / "mobile" / "third-party-policy.toml").read_text("utf-8"), "utf-8"
    )
    (tmp_path / "pnpm-lock.yaml").write_text(
        (repo_root / "pnpm-lock.yaml").read_text("utf-8"), "utf-8"
    )
    document = json.loads(notices.NOTICES.read_text("utf-8"))
    document["licences"][0]["packages"] = []
    (mobile / "public" / "third-party-notices.json").write_text(json.dumps(document), "utf-8")
    with pytest.raises(notices.CheckError, match=r"run app_notices\.py generate"):
        notices.check(repo=tmp_path)
    (mobile / "public" / "third-party-notices.json").unlink()
    with pytest.raises(notices.CheckError, match="is missing"):
        notices.check(repo=tmp_path)
