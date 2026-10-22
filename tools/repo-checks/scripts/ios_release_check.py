"""Checks that keep the iOS app shell uploadable to TestFlight, before any Mac minute is spent.

App Store Connect rejects an upload only after the build, the signing and the upload (ITMS
errors), so the TestFlight job (``.github/workflows/apple-testflight.yml``) runs these first, and
the repository tests (``tools/repo-checks/tests/test_ios_release.py``, CI job ``python``) run the
``source`` checks on every pull request without a macOS runner.

    python ios_release_check.py source
        The Xcode project, Info.plist, app icon and entitlements in apps/mobile/ios.

    python ios_release_check.py app <App.app> --version X.Y.Z --build N
        The built bundle: versions, compiled icon catalog, export compliance key, no Agora code.

    python ios_release_check.py build-number X.Y.Z [--rebuild N]
        CFBundleVersion of a release: major * 10000 + minor * 100 + patch, the Android versionCode
        of apps/mobile/android/app/build.gradle, with ".N" for a repeated upload of the same tag.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import struct
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
IOS = REPO / "apps" / "mobile" / "ios" / "App"
BUNDLE_ID = "app.mowdeck.mobile"
DEV_ENTITLEMENTS = IOS / "App" / "App.entitlements"
RELEASE_ENTITLEMENTS = (
    REPO / "apps" / "mobile" / "scripts" / "apple" / "entitlements" / "app-store.plist"
)
IPAD_ORIENTATIONS = {
    "UIInterfaceOrientationPortrait",
    "UIInterfaceOrientationPortraitUpsideDown",
    "UIInterfaceOrientationLandscapeLeft",
    "UIInterfaceOrientationLandscapeRight",
}
VERSION = re.compile(r"^(0|[1-9]\d{0,3})\.(0|[1-9]\d?)\.(0|[1-9]\d?)$")
# Text files of the bundle searched for the Agora SDK (it must never be redistributed,
# LICENSING.md); the shell bundles only its two pages, so a hit means a wrong web build.
TEXT_SUFFIXES = {".js", ".mjs", ".html", ".css", ".json", ".map", ".txt"}


class CheckError(Exception):
    """One or more problems, each reported as a GitHub error annotation."""


def build_number(version: str, rebuild: str = "") -> str:
    """The CFBundleVersion of a release version (minor and patch below 100)."""
    match = VERSION.match(version)
    if not match:
        msg = f"'{version}' is not a release version MAJOR.MINOR.PATCH (minor and patch below 100)"
        raise CheckError(msg)
    major, minor, patch = (int(part) for part in match.groups())
    code = str(major * 10000 + minor * 100 + patch)
    if rebuild:
        if not re.fullmatch(r"[1-9]\d?", rebuild):
            msg = f"rebuild must be empty or a number from 1 to 99, not '{rebuild}'"
            raise CheckError(msg)
        code += f".{rebuild}"
    return code


def png_info(path: Path) -> tuple[int, int, bool]:
    """Width, height and whether the PNG can carry transparency."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        msg = f"{path.name} is not a PNG"
        raise CheckError(msg)
    width, height, _depth, color_type = struct.unpack(">IIBB", data[16:26])
    # Colour types 4 and 6 have an alpha channel; a tRNS chunk adds transparency to the others.
    alpha = color_type in (4, 6) or b"tRNS" in data[: data.find(b"IDAT")]
    return width, height, alpha


def _load_plist(path: Path) -> dict[str, Any]:
    loaded = plistlib.loads(path.read_bytes())
    if not isinstance(loaded, dict):
        msg = f"{path} is not a dictionary plist"
        raise CheckError(msg)
    return loaded


def check_icons(appiconset: Path) -> list[str]:
    """The App Store icon: one 1024x1024 PNG without transparency (ITMS-90717, ITMS-90704)."""
    problems = []
    contents = appiconset / "Contents.json"
    if not contents.is_file():
        return [f"{appiconset} has no Contents.json"]
    images = json.loads(contents.read_text("utf-8")).get("images", [])
    marketing = [
        img
        for img in images
        if img.get("size") == "1024x1024"
        and img.get("idiom") in ("universal", "ios-marketing")
        and img.get("filename")
    ]
    if not marketing:
        return ["AppIcon has no 1024x1024 image (the App Store icon)"]
    for img in marketing:
        path = appiconset / img["filename"]
        if not path.is_file():
            problems.append(f"AppIcon image {img['filename']} is missing")
            continue
        width, height, alpha = png_info(path)
        if (width, height) != (1024, 1024):
            problems.append(f"AppIcon {img['filename']} is {width}x{height}, expected 1024x1024")
        if alpha:
            problems.append(
                f"AppIcon {img['filename']} has an alpha channel; App Store Connect rejects a "
                "transparent App Store icon (ITMS-90717), save it as RGB"
            )
    return problems


def check_info_plist(info: dict[str, Any]) -> list[str]:
    """Info.plist keys App Store Connect needs or TestFlight would otherwise wait on."""
    problems = []
    if info.get("ITSAppUsesNonExemptEncryption") is not False:
        problems.append(
            "Info.plist: ITSAppUsesNonExemptEncryption must be false (standard HTTPS only), "
            "otherwise every TestFlight build waits for the export compliance answer"
        )
    if info.get("CFBundleVersion") != "$(CURRENT_PROJECT_VERSION)":
        problems.append("Info.plist: CFBundleVersion must be $(CURRENT_PROJECT_VERSION)")
    if info.get("CFBundleShortVersionString") != "$(MARKETING_VERSION)":
        problems.append("Info.plist: CFBundleShortVersionString must be $(MARKETING_VERSION)")
    if not info.get("UILaunchStoryboardName"):
        problems.append("Info.plist: UILaunchStoryboardName is required (ITMS-90475)")
    ipad = set(info.get("UISupportedInterfaceOrientations~ipad") or [])
    if not info.get("UIRequiresFullScreen") and ipad != IPAD_ORIENTATIONS:
        problems.append(
            "Info.plist: an iPad app with multitasking must support all four orientations "
            "(UISupportedInterfaceOrientations~ipad) or set UIRequiresFullScreen (ITMS-90474)"
        )
    return problems


def check_entitlements(dev: dict[str, Any], release: dict[str, Any]) -> list[str]:
    """The release entitlements mirror the development ones, with production APNs."""
    problems = []
    if set(dev) != set(release):
        problems.append(
            "the entitlements of the App Store build (apps/mobile/scripts/apple/entitlements/"
            f"app-store.plist: {sorted(release)}) differ from App.entitlements ({sorted(dev)}); "
            "a capability added to the app must be added to both"
        )
    if release.get("aps-environment") != "production":
        problems.append("app-store.plist: aps-environment must be production (TestFlight)")
    return problems


def check_project(pbxproj: str) -> list[str]:
    """Build settings of the app target: iPhone and iPad, the bundle id, no Mac Catalyst."""
    problems = []
    if f"PRODUCT_BUNDLE_IDENTIFIER = {BUNDLE_ID};" not in pbxproj:
        problems.append(f"App.xcodeproj: PRODUCT_BUNDLE_IDENTIFIER is not {BUNDLE_ID}")
    families = set(re.findall(r'TARGETED_DEVICE_FAMILY = "?([0-9,]+)"?;', pbxproj))
    if families != {"1,2"}:
        problems.append(
            f"App.xcodeproj: TARGETED_DEVICE_FAMILY is {sorted(families)}, expected 1,2 "
            "(iPhone and iPad; Apple silicon Macs run the iPad app)"
        )
    if "SUPPORTS_MACCATALYST = YES" in pbxproj:
        problems.append("App.xcodeproj: Mac Catalyst is not supported (SUPPORTS_MACCATALYST)")
    return problems


def check_source(ios: Path = IOS, release_entitlements: Path = RELEASE_ENTITLEMENTS) -> None:
    """Every static check of the app shell; raises CheckError with all problems."""
    problems = check_info_plist(_load_plist(ios / "App" / "Info.plist"))
    problems += check_icons(ios / "App" / "Assets.xcassets" / "AppIcon.appiconset")
    problems += check_entitlements(
        _load_plist(ios / "App" / "App.entitlements"), _load_plist(release_entitlements)
    )
    problems += check_project((ios / "App.xcodeproj" / "project.pbxproj").read_text("utf-8"))
    if problems:
        raise CheckError("\n".join(problems))


def check_app(app: Path, version: str, build: str) -> None:
    """The built App.app before it is signed."""
    info = _load_plist(app / "Info.plist")
    problems = []
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        problems.append(f"CFBundleIdentifier is {info.get('CFBundleIdentifier')!r}")
    if info.get("CFBundleShortVersionString") != version:
        problems.append(f"CFBundleShortVersionString is {info.get('CFBundleShortVersionString')!r}")
    if info.get("CFBundleVersion") != build:
        problems.append(f"CFBundleVersion is {info.get('CFBundleVersion')!r}, expected {build!r}")
    if info.get("ITSAppUsesNonExemptEncryption") is not False:
        problems.append("ITSAppUsesNonExemptEncryption is not false")
    if info.get("CFBundleIconName") != "AppIcon" or not (app / "Assets.car").is_file():
        problems.append(
            "no compiled app icon (CFBundleIconName AppIcon and Assets.car): App Store Connect "
            "rejects the build (ITMS-90713)"
        )
    if not (app / "public" / "index.html").is_file():
        problems.append("the bundled pages are missing (public/index.html): cap sync did not run")
    for path in app.rglob("*"):
        rel = path.relative_to(app).as_posix()
        if "agora" in rel.lower():
            problems.append(f"{rel}: Agora files must never be shipped (LICENSING.md)")
        elif (
            path.is_file()
            and path.suffix in TEXT_SUFFIXES
            and b"agora" in path.read_bytes().lower()
        ):
            problems.append(f"{rel} mentions Agora: the shell must not bundle the web app")
    if problems:
        raise CheckError("\n".join(problems))


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="TestFlight readiness of the iOS app shell")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("source")
    a = sub.add_parser("app")
    a.add_argument("app", type=Path)
    a.add_argument("--version", required=True)
    a.add_argument("--build", required=True)
    b = sub.add_parser("build-number")
    b.add_argument("version")
    b.add_argument("--rebuild", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "source":
            check_source()
            print("iOS app shell: Info.plist, icon, entitlements and project are App Store ready.")
        elif args.command == "app":
            check_app(args.app, args.version, args.build)
            print(f"{args.app}: {args.version} ({args.build}), icon catalog, no Agora files.")
        else:
            print(build_number(args.version, args.rebuild))
    except CheckError as exc:
        for line in str(exc).splitlines():
            print(f"::error::{line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
