"""Provisioning profile checks and signing entitlements for the TestFlight job.

Used by ``apps/mobile/scripts/apple/release-ios.sh`` (``.github/workflows/apple-testflight.yml``).
Standard library only and Python 3.9 compatible (the macOS runner's python3), so the tests
(``tools/repo-checks/tests/test_ios_release.py``) run on any runner.

A provisioning profile is a CMS (PKCS#7) envelope around an XML plist. Reading it here does not
verify Apple's CMS signature (codesign and App Store Connect do that); it only extracts the plist
to make sure the right profile was supplied for the right bundle.

    python ios_signing.py profile <file>
        Print a JSON summary (name, team, bundle id, kind, expiry, entitlements).

    python ios_signing.py check-profile <file> --bundle-id ID --team TEAM
        Fail with a readable message unless it is a valid iOS App Store profile for TEAM.ID.

    python ios_signing.py entitlements --declared PLIST --profile FILE --out PLIST
        Write the entitlements to sign with: the declared ones (apps/mobile/scripts/apple/
        entitlements/app-store.plist) plus the identity keys the profile grants (application
        identifier, team, get-task-allow, beta-reports-active). Every declared entitlement must be
        granted by the profile, otherwise the command fails and names the capability to enable on
        the App ID.

    python ios_signing.py find-profile <dir> --bundle-id ID --team TEAM
        Print the path of the one profile in <dir> whose application identifier is TEAM.ID.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import plistlib
import sys
from pathlib import Path
from typing import Any

# Keys copied from the profile into the signing entitlements (what Xcode adds on export).
IDENTITY_KEYS = (
    "application-identifier",
    "com.apple.developer.team-identifier",
    "get-task-allow",
    "beta-reports-active",
)


class SigningError(Exception):
    """A profile or entitlement problem, reported as a GitHub error annotation."""


def read_profile(path: Path) -> dict[str, Any]:
    """The plist inside a provisioning profile's CMS envelope."""
    data = path.read_bytes()
    start = data.find(b"<?xml")
    end = data.find(b"</plist>")
    if start < 0 or end < 0:
        msg = f"{path}: not a provisioning profile (no embedded plist found)"
        raise SigningError(msg)
    try:
        loaded = plistlib.loads(data[start : end + len(b"</plist>")])
    except Exception as exc:  # plistlib raises several types
        msg = f"{path}: embedded plist does not parse: {exc}"
        raise SigningError(msg) from exc
    if not isinstance(loaded, dict):
        msg = f"{path}: embedded plist is not a dictionary"
        raise SigningError(msg)
    return loaded


def _entitlements(profile: dict[str, Any]) -> dict[str, Any]:
    ents = profile.get("Entitlements")
    return ents if isinstance(ents, dict) else {}


def profile_kind(profile: dict[str, Any]) -> str:
    """app-store, development, ad-hoc or enterprise."""
    debuggable = _entitlements(profile).get("get-task-allow") is True
    if profile.get("ProvisionsAllDevices"):
        return "enterprise"
    if profile.get("ProvisionedDevices"):
        return "development" if debuggable else "ad-hoc"
    return "development" if debuggable else "app-store"


def summary(profile: dict[str, Any]) -> dict[str, Any]:
    """The facts the checks and the job log need."""
    team_ids = profile.get("TeamIdentifier") or []
    team = str(team_ids[0]) if team_ids else ""
    appid = str(_entitlements(profile).get("application-identifier", ""))
    bundle = appid[len(team) + 1 :] if team and appid.startswith(team + ".") else appid
    expires = profile.get("ExpirationDate")
    return {
        "name": str(profile.get("Name", "")),
        "uuid": str(profile.get("UUID", "")),
        "team": team,
        "app_identifier": appid,
        "bundle_id": bundle,
        "kind": profile_kind(profile),
        "expires": expires.isoformat() if isinstance(expires, dt.datetime) else "",
        "entitlements": sorted(_entitlements(profile)),
    }


def check_profile(
    profile: dict[str, Any],
    path: str,
    bundle_id: str,
    team: str,
    now: dt.datetime | None = None,
) -> list[str]:
    """Return warnings; raise SigningError unless it is a valid App Store profile for the app."""
    info = summary(profile)
    problems = []
    if info["team"] != team:
        problems.append(f"team is {info['team'] or 'missing'}, expected {team}")
    if info["app_identifier"] != f"{team}.{bundle_id}":
        problems.append(
            f"app identifier is {info['app_identifier'] or 'missing'}, expected {team}.{bundle_id}"
        )
    if info["kind"] != "app-store":
        problems.append(
            f"profile type is {info['kind']}, expected an App Store profile "
            "(Distribution -> App Store Connect)"
        )
    # A watch app's App ID is an iOS App ID too (ADR 0060); "watchOS" is accepted in case Apple
    # ever lists it separately.
    platforms = profile.get("Platform") or []
    if platforms and not {"iOS", "watchOS"} & set(platforms):
        problems.append(f"platform is {', '.join(map(str, platforms))}, expected iOS")
    # timezone.utc, not dt.UTC: the macOS runner's python3 may be older than 3.11.
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)  # noqa: UP017
    warnings = []
    expires = profile.get("ExpirationDate")
    if isinstance(expires, dt.datetime):
        exp = expires.replace(tzinfo=None)
        if exp <= now:
            problems.append(f"expired on {exp:%Y-%m-%d}")
        elif exp - now < dt.timedelta(days=30):
            warnings.append(f"{path} ({info['name']}) expires on {exp:%Y-%m-%d}: renew it soon")
    if problems:
        raise SigningError(f"{path} ({info['name'] or 'unnamed'}): " + "; ".join(problems))
    return warnings


def _allowed(declared: object, granted: object) -> bool:
    if isinstance(declared, bool):
        return granted is True or declared is False
    if isinstance(declared, str):
        if isinstance(granted, list):
            return any(fnmatch.fnmatchcase(declared, g) for g in granted if isinstance(g, str))
        return isinstance(granted, str) and fnmatch.fnmatchcase(declared, granted)
    if isinstance(declared, list):
        return all(_allowed(item, granted) for item in declared)
    return declared == granted


def _expand_prefix(value: Any, prefix: str) -> Any:
    """``$(AppIdentifierPrefix)`` (team id and a dot, as Xcode expands it) in strings and lists."""
    if isinstance(value, str):
        return value.replace("$(AppIdentifierPrefix)", prefix)
    if isinstance(value, list):
        return [_expand_prefix(item, prefix) for item in value]
    return value


def build_entitlements(declared: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """The declared entitlements plus the profile's identity keys; every declared key granted.

    ``$(AppIdentifierPrefix)`` in a declared value becomes the profile's team id and a dot
    (the keychain access group of the widget credential, ADR 0059).
    """
    granted = _entitlements(profile)
    teams = profile.get("TeamIdentifier") or []
    prefix = f"{teams[0]}." if teams else ""
    declared = {key: _expand_prefix(value, prefix) for key, value in declared.items()}
    missing = []
    for key, value in declared.items():
        if key not in granted:
            missing.append(
                f"{key} (not in the profile: enable the capability on the App ID, "
                "then edit and download the App Store profile again)"
            )
        elif not _allowed(value, granted[key]):
            missing.append(f"{key}={value!r} (profile allows {granted[key]!r})")
    if missing:
        name = profile.get("Name", "")
        raise SigningError(f"profile '{name}' does not grant: " + "; ".join(missing))
    result = dict(declared)
    for key in IDENTITY_KEYS:
        if key in granted:
            result[key] = granted[key]
    if result.get("get-task-allow") is True:
        msg = "the profile allows get-task-allow: that is a development profile, not App Store"
        raise SigningError(msg)
    return result


def find_profile(directory: Path, bundle_id: str, team: str) -> Path:
    """The single profile in ``directory`` for TEAM.BUNDLE_ID."""
    wanted = f"{team}.{bundle_id}"
    matches = [
        path
        for path in sorted(directory.iterdir())
        if path.suffix == ".mobileprovision"
        and _entitlements(read_profile(path)).get("application-identifier") == wanted
    ]
    if not matches:
        msg = f"no profile for {wanted} in {directory}"
        raise SigningError(msg)
    if len(matches) > 1:
        msg = f"more than one profile for {wanted}: {', '.join(map(str, matches))}"
        raise SigningError(msg)
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="iOS App Store signing helpers")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("profile")
    p.add_argument("file", type=Path)

    c = sub.add_parser("check-profile")
    c.add_argument("file", type=Path)
    c.add_argument("--bundle-id", required=True)
    c.add_argument("--team", required=True)

    e = sub.add_parser("entitlements")
    e.add_argument("--declared", required=True, type=Path)
    e.add_argument("--profile", required=True, type=Path)
    e.add_argument("--out", required=True, type=Path)

    f = sub.add_parser("find-profile")
    f.add_argument("directory", type=Path)
    f.add_argument("--bundle-id", required=True)
    f.add_argument("--team", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "profile":
            print(json.dumps(summary(read_profile(args.file)), indent=2))
        elif args.command == "check-profile":
            profile = read_profile(args.file)
            for warning in check_profile(profile, str(args.file), args.bundle_id, args.team):
                print(f"::warning::{warning}")
            info = summary(profile)
            print(f"Profile '{info['name']}' ({info['app_identifier']}, expires {info['expires']})")
        elif args.command == "entitlements":
            declared = plistlib.loads(args.declared.read_bytes())
            result = build_entitlements(declared, read_profile(args.profile))
            args.out.write_bytes(plistlib.dumps(result))
        elif args.command == "find-profile":
            print(find_profile(args.directory, args.bundle_id, args.team))
    except SigningError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
