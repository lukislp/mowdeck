# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Checks that keep the iOS app shell uploadable to TestFlight, before any Mac minute is spent.

App Store Connect rejects an upload only after the build, the signing and the upload (ITMS
errors), so the TestFlight job (``.github/workflows/apple-testflight.yml``) runs these first, and
the repository tests (``tools/repo-checks/tests/test_ios_release.py``, CI job ``python``) run the
``source`` checks on every pull request without a macOS runner.

    python ios_release_check.py source
        The Xcode project, Info.plist, app icon and entitlements in apps/mobile/ios, and the
        privacy manifest of every target that uses a required-reason API (ITMS-91053).

    python ios_release_check.py app <App.app> --version X.Y.Z --build N
        The built bundle: versions, compiled icon catalog, export compliance key, no Agora code,
        a privacy manifest in every bundle that needs one, and the About page with the
        third-party notices (apps/mobile/public/third-party-notices.json, app_notices.py).

    python ios_release_check.py build-number X.Y.Z [--rebuild N]
        CFBundleVersion of a release: major * 10000 + minor * 100 + patch, the Android versionCode
        of apps/mobile/android/app/build.gradle, with ".N" for a repeated upload of the same tag.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import struct
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

REPO = Path(__file__).resolve().parents[3]
IOS = REPO / "apps" / "mobile" / "ios" / "App"
NOTICES = REPO / "apps" / "mobile" / "public" / "third-party-notices.json"
BUNDLE_ID = "app.mowdeck.mobile"
EXTENSION_BUNDLE_ID = f"{BUNDLE_ID}.NotificationService"
EXTENSION_NAME = "NotificationService"
WIDGETS_BUNDLE_ID = f"{BUNDLE_ID}.Widgets"
WIDGETS_NAME = "MowDeckWidgets"
WIDGETS_POINT = "com.apple.widgetkit-extension"
WATCH_BUNDLE_ID = f"{BUNDLE_ID}.watchkitapp"
WATCH_NAME = "MowDeckWatch"
WATCH_WIDGETS_BUNDLE_ID = f"{WATCH_BUNDLE_ID}.Widgets"
WATCH_WIDGETS_NAME = "MowDeckWatchWidgets"
APP_GROUP = "group.app.mowdeck.mobile"
KEYCHAIN_GROUP = "$(AppIdentifierPrefix)app.mowdeck.mobile.shared"
DEV_ENTITLEMENTS = IOS / "App" / "App.entitlements"
RELEASE_ENTITLEMENTS = (
    REPO / "apps" / "mobile" / "scripts" / "apple" / "entitlements" / "app-store.plist"
)
EXTENSION_POINT = "com.apple.usernotifications.service"
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


def check_extension_plist(info: dict[str, Any]) -> list[str]:
    """Info.plist of the Notification Service Extension (camera photo in error and theft pushes,
    ADR 0056): it must be a notification service and carry the app's version, or App Store Connect
    rejects the upload."""
    problems = []
    extension = info.get("NSExtension")
    extension = extension if isinstance(extension, dict) else {}
    if extension.get("NSExtensionPointIdentifier") != EXTENSION_POINT:
        problems.append(
            f"NotificationService/Info.plist: NSExtensionPointIdentifier must be {EXTENSION_POINT}"
        )
    if extension.get("NSExtensionPrincipalClass") != "$(PRODUCT_MODULE_NAME).NotificationService":
        problems.append(
            "NotificationService/Info.plist: NSExtensionPrincipalClass must be "
            "$(PRODUCT_MODULE_NAME).NotificationService"
        )
    if info.get("CFBundleVersion") != "$(CURRENT_PROJECT_VERSION)":
        problems.append(
            "NotificationService/Info.plist: CFBundleVersion must be $(CURRENT_PROJECT_VERSION)"
        )
    if info.get("CFBundleShortVersionString") != "$(MARKETING_VERSION)":
        problems.append(
            "NotificationService/Info.plist: "
            "CFBundleShortVersionString must be $(MARKETING_VERSION)"
        )
    if info.get("ITSAppUsesNonExemptEncryption") is not False:
        problems.append(
            "NotificationService/Info.plist: ITSAppUsesNonExemptEncryption must be false"
        )
    return problems


def check_widgets_plist(info: dict[str, Any], name: str = WIDGETS_NAME) -> list[str]:
    """Info.plist of a WidgetKit extension: the iPhone widgets and the mowing Live Activity
    (ADR 0059) or the watch complication (``name`` MowDeckWatchWidgets, ADR 0060)."""
    problems = []
    extension = info.get("NSExtension")
    extension = extension if isinstance(extension, dict) else {}
    if extension.get("NSExtensionPointIdentifier") != WIDGETS_POINT:
        problems.append(f"{name}/Info.plist: NSExtensionPointIdentifier must be {WIDGETS_POINT}")
    if info.get("CFBundleVersion") != "$(CURRENT_PROJECT_VERSION)":
        problems.append(f"{name}/Info.plist: CFBundleVersion must be $(CURRENT_PROJECT_VERSION)")
    if info.get("CFBundleShortVersionString") != "$(MARKETING_VERSION)":
        problems.append(
            f"{name}/Info.plist: CFBundleShortVersionString must be $(MARKETING_VERSION)"
        )
    if info.get("ITSAppUsesNonExemptEncryption") is not False:
        problems.append(f"{name}/Info.plist: ITSAppUsesNonExemptEncryption must be false")
    return problems


def check_watch_plist(info: dict[str, Any]) -> list[str]:
    """Info.plist of the watch app (ADR 0060): a single-target watch app that belongs to the
    iPhone app (it relays every command through it), with the app's version."""
    problems = []
    if info.get("WKApplication") is not True:
        problems.append("MowDeckWatch/Info.plist: WKApplication must be true (single-target app)")
    if info.get("WKCompanionAppBundleIdentifier") != BUNDLE_ID:
        problems.append(
            f"MowDeckWatch/Info.plist: WKCompanionAppBundleIdentifier must be {BUNDLE_ID}"
        )
    if info.get("WKRunsIndependentlyOfCompanionApp") is not False:
        problems.append(
            "MowDeckWatch/Info.plist: WKRunsIndependentlyOfCompanionApp must be false (the watch "
            "relays through the iPhone app)"
        )
    if "WKWatchKitApp" in info:
        problems.append("MowDeckWatch/Info.plist: WKWatchKitApp is the old two-target layout")
    if info.get("CFBundleVersion") != "$(CURRENT_PROJECT_VERSION)":
        problems.append(
            "MowDeckWatch/Info.plist: CFBundleVersion must be $(CURRENT_PROJECT_VERSION)"
        )
    if info.get("CFBundleShortVersionString") != "$(MARKETING_VERSION)":
        problems.append(
            "MowDeckWatch/Info.plist: CFBundleShortVersionString must be $(MARKETING_VERSION)"
        )
    if info.get("ITSAppUsesNonExemptEncryption") is not False:
        problems.append("MowDeckWatch/Info.plist: ITSAppUsesNonExemptEncryption must be false")
    return problems


def check_watch_entitlements(entitlements: dict[str, dict[str, Any]]) -> list[str]:
    """The watch targets (development and App Store files alike) have the App Group, which the
    complication reads, and nothing else: the watch holds no credential (ADR 0060), so a
    Keychain group or any other capability there is a mistake."""
    problems = []
    for name, ents in entitlements.items():
        if (ents.get("com.apple.security.application-groups") or []) != [APP_GROUP]:
            problems.append(f"{name}: the App Group must be exactly [{APP_GROUP}]")
        extra = sorted(set(ents) - {"com.apple.security.application-groups"})
        if extra:
            problems.append(f"{name}: the watch needs no other entitlement, found {extra}")
    return problems


def check_widget_entitlements(app: dict[str, Any], widgets: dict[str, Any]) -> list[str]:
    """The app and the widget extension must share the App Group and the Keychain group, or the
    widgets cannot read the credential the app stores (ADR 0059)."""
    problems = []
    for name, entitlements in (("App.entitlements", app), ("widgets entitlements", widgets)):
        if APP_GROUP not in (entitlements.get("com.apple.security.application-groups") or []):
            problems.append(f"{name}: the App Group {APP_GROUP} is missing")
        groups = entitlements.get("keychain-access-groups") or []
        if not groups or groups[0] != KEYCHAIN_GROUP:
            problems.append(f"{name}: the first keychain access group must be {KEYCHAIN_GROUP}")
    return problems


_STRING_ENTRY = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;', re.M)
LOCALISATIONS = ("en", "de")
LOCAL_NETWORK_KEY = "NSLocalNetworkUsageDescription"


def parse_strings(text: str) -> dict[str, str]:
    """Key and value of an old-style ``.strings`` file (comments dropped; no escapes beyond the
    quote and backslash that the localised Info.plist texts could need)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return {
        key: value.replace('\\"', '"').replace("\\\\", "\\")
        for key, value in _STRING_ENTRY.findall(text)
    }


def check_localised_info(info: dict[str, Any], app_dir: Path, pbxproj: str) -> list[str]:
    """The local network prompt text exists in every localisation the project knows (en, de),
    is wired into the app target as the variant group InfoPlist.strings, and the English text
    is the Info.plist default, so a device in any other language reads the same as before."""
    problems = []
    texts: dict[str, str] = {}
    for language in LOCALISATIONS:
        path = app_dir / f"{language}.lproj" / "InfoPlist.strings"
        if not path.is_file():
            problems.append(f"App/{language}.lproj/InfoPlist.strings is missing")
            continue
        value = parse_strings(path.read_text("utf-8")).get(LOCAL_NETWORK_KEY, "").strip()
        if not value:
            problems.append(f"{language}.lproj/InfoPlist.strings has no {LOCAL_NETWORK_KEY}")
        texts[language] = value
        if f"path = {language}.lproj/InfoPlist.strings;" not in pbxproj:
            problems.append(f"App.xcodeproj: {language}.lproj/InfoPlist.strings is not referenced")
    if "InfoPlist.strings in Resources" not in pbxproj or "isa = PBXVariantGroup" not in pbxproj:
        problems.append("App.xcodeproj: InfoPlist.strings is not a resource of the app target")
    if texts.get("en") and texts["en"] != str(info.get(LOCAL_NETWORK_KEY, "")).strip():
        problems.append("en.lproj/InfoPlist.strings differs from the default in Info.plist")
    if texts.get("de") and texts.get("de") == texts.get("en"):
        problems.append("de.lproj/InfoPlist.strings is not translated")
    return problems


def check_project(pbxproj: str) -> list[str]:
    """Build settings of the app target: iPhone and iPad, the bundle id, no Mac Catalyst; the
    Notification Service Extension target, embedded in the app."""
    problems = []
    if f"PRODUCT_BUNDLE_IDENTIFIER = {BUNDLE_ID};" not in pbxproj:
        problems.append(f"App.xcodeproj: PRODUCT_BUNDLE_IDENTIFIER is not {BUNDLE_ID}")
    if f"PRODUCT_BUNDLE_IDENTIFIER = {EXTENSION_BUNDLE_ID};" not in pbxproj:
        problems.append(
            f"App.xcodeproj: the extension's PRODUCT_BUNDLE_IDENTIFIER is not {EXTENSION_BUNDLE_ID}"
        )
    if f"PRODUCT_BUNDLE_IDENTIFIER = {WIDGETS_BUNDLE_ID};" not in pbxproj:
        problems.append(
            "App.xcodeproj: the widget extension's PRODUCT_BUNDLE_IDENTIFIER is not "
            f"{WIDGETS_BUNDLE_ID}"
        )
    if "MowDeckWidgets.appex in Embed Foundation Extensions" not in pbxproj:
        problems.append("App.xcodeproj: the app does not embed the MowDeckWidgets extension")
    if "com.apple.product-type.app-extension" not in pbxproj:
        problems.append("App.xcodeproj: the NotificationService app extension target is missing")
    if "dstSubfolderSpec = 13;" not in pbxproj or "Embed Foundation Extensions" not in pbxproj:
        problems.append("App.xcodeproj: the app does not embed the NotificationService extension")
    # The Apple Watch app (ADR 0060): embedded in App.app/Watch, its complication in its PlugIns.
    for bundle in (WATCH_BUNDLE_ID, WATCH_WIDGETS_BUNDLE_ID):
        if f"PRODUCT_BUNDLE_IDENTIFIER = {bundle};" not in pbxproj:
            problems.append(f"App.xcodeproj: no target with PRODUCT_BUNDLE_IDENTIFIER {bundle}")
    if (
        "MowDeckWatch.app in Embed Watch Content" not in pbxproj
        or 'dstPath = "$(CONTENTS_FOLDER_PATH)/Watch";' not in pbxproj
        or "dstSubfolderSpec = 16;" not in pbxproj
    ):
        problems.append("App.xcodeproj: the app does not embed MowDeckWatch.app in Watch/")
    if "MowDeckWatchWidgets.appex in Embed Foundation Extensions" not in pbxproj:
        problems.append("App.xcodeproj: the watch app does not embed its complication extension")
    if "SDKROOT = watchos;" not in pbxproj or "WATCHOS_DEPLOYMENT_TARGET = " not in pbxproj:
        problems.append("App.xcodeproj: the watch targets must build for watchOS")
    families = set(re.findall(r'TARGETED_DEVICE_FAMILY = "?([0-9,]+)"?;', pbxproj))
    if families != {"1,2", "4"}:
        problems.append(
            f"App.xcodeproj: TARGETED_DEVICE_FAMILY is {sorted(families)}, expected 1,2 "
            "(iPhone and iPad; Apple silicon Macs run the iPad app) and 4 (the watch targets)"
        )
    if "SUPPORTS_MACCATALYST = YES" in pbxproj:
        problems.append("App.xcodeproj: Mac Catalyst is not supported (SUPPORTS_MACCATALYST)")
    return problems


_OBJECT = re.compile(r"^\t\t([0-9A-F]{24})(?: /\*.*?\*/)? = \{(.*)$")
_ID = re.compile(r"\b[0-9A-F]{24}\b")
# Written by `cap sync ios` (ignored by git), so absent in a fresh checkout.
GENERATED = {"public", "config.xml", "capacitor.config.json"}


def _objects(pbxproj: str) -> dict[str, str]:
    """Object id -> body text of every object of a project file (one-line and block form)."""
    objects: dict[str, str] = {}
    lines = pbxproj.splitlines()
    i = 0
    while i < len(lines):
        match = _OBJECT.match(lines[i])
        if not match:
            i += 1
            continue
        oid, rest = match.groups()
        if rest.rstrip().endswith("};"):
            body = rest
        else:
            block = []
            i += 1
            while i < len(lines) and lines[i] != "\t\t};":
                block.append(lines[i])
                i += 1
            body = "\n".join(block)
        if oid in objects:
            objects[oid + "#duplicate"] = body
        objects[oid] = body
        i += 1
    return objects


def _field(body: str, name: str) -> str | None:
    match = re.search(rf"\b{name} = (\"(?:[^\"\\]|\\.)*\"|[^;]+);", body)
    return match.group(1).strip('"') if match else None


def check_project_integrity(pbxproj: str) -> list[str]:
    """Every object id that is referenced is defined, and no id is defined twice: the kind of
    mistake a hand-edited project file makes and only Xcode would otherwise notice."""
    objects = _objects(pbxproj)
    problems = [
        f"App.xcodeproj: object {key.split('#')[0]} is defined twice"
        for key in objects
        if key.endswith("#duplicate")
    ]
    defined = {key for key in objects if "#" not in key}
    for body in [b for k, b in objects.items() if "#" not in k]:
        for ref in _ID.findall(body):
            if ref not in defined:
                problems.append(f"App.xcodeproj: object {ref} is referenced but not defined")
    root = re.search(r"rootObject = ([0-9A-F]{24})", pbxproj)
    if not root or root.group(1) not in defined:
        problems.append("App.xcodeproj: the root object is missing")
    return sorted(set(problems))


def check_project_files(pbxproj: str, project_dir: Path) -> list[str]:
    """Every file the groups name exists (resolved through the group paths from the main group),
    and so does every Info.plist and entitlements file the build settings name. Paths are
    relative to the folder of App.xcodeproj; the extension and watch sources live next to it."""
    objects = _objects(pbxproj)
    problems = []
    project = next((b for b in objects.values() if "isa = PBXProject;" in b), "")
    main = _field(project, "mainGroup")

    def walk(oid: str, base: Path) -> None:
        body = objects.get(oid, "")
        isa = _field(body, "isa")
        tree = _field(body, "sourceTree")
        path = _field(body, "path")
        if tree == "BUILT_PRODUCTS_DIR":
            return
        if tree == "SOURCE_ROOT":
            here = project_dir / path if path else project_dir
        elif tree == "<group>":
            here = base / path if path else base
        else:
            return
        here = Path(os.path.normpath(here))
        if isa in ("PBXGroup", "PBXVariantGroup"):
            if isa == "PBXGroup" and path and not here.is_dir():
                problems.append(
                    f"App.xcodeproj: group folder {here.relative_to(REPO).as_posix()} is missing"
                )
                return
            children = re.search(r"children = \((.*?)\);", body, re.DOTALL)
            for child in _ID.findall(children.group(1) if children else ""):
                walk(child, here)
        elif isa == "PBXFileReference" and path and path not in GENERATED and not here.exists():
            problems.append(f"App.xcodeproj: {here.relative_to(REPO).as_posix()} is missing")

    if main:
        walk(main, project_dir)
    for setting in ("INFOPLIST_FILE", "CODE_SIGN_ENTITLEMENTS"):
        for value in sorted(set(re.findall(rf"{setting} = \"?([^\";]+)\"?;", pbxproj))):
            if not (project_dir / value).is_file():
                problems.append(f"App.xcodeproj: {setting} {value} does not exist")
    return problems


class Target(NamedTuple):
    """One shipped bundle: the folder of its Info.plist and privacy manifest, the Swift sources
    it compiles (local packages it links included), both relative to apps/mobile/ios, and where
    the built bundle sits inside App.app."""

    name: str
    folder: str
    sources: tuple[str, ...]
    built: str


# The bundles of the app, the one list the privacy manifest checks walk. Every target but the
# notification extension links MowerWidgetKit (App Group state, ADR 0059/0060). Third-party
# plugin sources (node_modules) are not scanned: a plugin without a manifest of its own is
# covered by the manifest of the bundle it is compiled into (docs/ops/mobile-apps.md).
_KIT = "MowerWidgetKit/Sources"
TARGETS = (
    Target("App", "App/App", ("App/App", _KIT), ""),
    Target(
        EXTENSION_NAME,
        f"App/{EXTENSION_NAME}",
        (f"App/{EXTENSION_NAME}", "NotificationServiceKit/Sources"),
        f"PlugIns/{EXTENSION_NAME}.appex",
    ),
    Target(WIDGETS_NAME, WIDGETS_NAME, (WIDGETS_NAME, _KIT), f"PlugIns/{WIDGETS_NAME}.appex"),
    Target(WATCH_NAME, WATCH_NAME, (WATCH_NAME, _KIT), f"Watch/{WATCH_NAME}.app"),
    Target(
        WATCH_WIDGETS_NAME,
        WATCH_WIDGETS_NAME,
        (WATCH_WIDGETS_NAME, _KIT),
        f"Watch/{WATCH_NAME}.app/PlugIns/{WATCH_WIDGETS_NAME}.appex",
    ),
)
MANIFEST = "PrivacyInfo.xcprivacy"
# Required-reason API categories (Apple, "Describing use of required reason API"): the source
# patterns that use one, and the approved reason codes. Add a pattern when the code starts using
# another API of a category; the manifest must then declare it.
REQUIRED_REASON_APIS: dict[str, tuple[re.Pattern[str], frozenset[str]]] = {
    "NSPrivacyAccessedAPICategoryUserDefaults": (
        re.compile(r"\bUserDefaults\b|\bNSUserDefaults\b"),
        frozenset({"CA92.1", "1C8F.1", "C56D.1", "AC6B.1"}),
    ),
    "NSPrivacyAccessedAPICategoryFileTimestamp": (
        re.compile(
            r"creationDate|modificationDate|NSFileCreationDate|NSFileModificationDate"
            r"|contentModificationDateKey|\bgetattrlist\b|\bf?l?stat\("
        ),
        frozenset({"DDA9.1", "C617.1", "3B52.1", "0A2A.1"}),
    ),
    "NSPrivacyAccessedAPICategorySystemBootTime": (
        re.compile(r"systemUptime|mach_absolute_time|clock_gettime"),
        frozenset({"35F9.1", "8FFB.1", "3D61.1"}),
    ),
    "NSPrivacyAccessedAPICategoryDiskSpace": (
        re.compile(
            r"volumeAvailableCapacity|volumeTotalCapacity|systemFreeSize|NSFileSystem(Free)?Size"
            r"|\bstatf?s\(|\bstatvfs\("
        ),
        frozenset({"85F4.1", "E174.1", "7D9E.1", "B728.1"}),
    ),
    "NSPrivacyAccessedAPICategoryActiveKeyboards": (
        re.compile(r"activeInputModes"),
        frozenset({"3EC4.1", "54BD.1"}),
    ),
}


def used_required_reason_apis(sources: Iterable[Path]) -> dict[str, list[str]]:
    """Category -> names of the Swift/Objective-C files under ``sources`` that use an API of it.
    Comment-only lines are ignored."""
    used: dict[str, set[str]] = {}
    for root in sources:
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".swift", ".m", ".mm", ".h"}:
                continue
            code = "\n".join(
                line
                for line in path.read_text("utf-8").splitlines()
                if not line.lstrip().startswith("//")
            )
            for category, (pattern, _reasons) in REQUIRED_REASON_APIS.items():
                if pattern.search(code):
                    used.setdefault(category, set()).add(path.name)
    return {category: sorted(files) for category, files in used.items()}


def check_privacy_manifest(manifest: dict[str, Any], used: Iterable[str], label: str) -> list[str]:
    """A privacy manifest that declares exactly the required-reason API categories ``used``, each
    with approved reason codes, no tracking and no collected data (MowDeck sends nothing to its
    developer, docs/ops/mobile-apps.md)."""
    used = set(used)
    problems = []
    if manifest.get("NSPrivacyTracking") is not False:
        problems.append(f"{label}: NSPrivacyTracking must be false")
    if manifest.get("NSPrivacyTrackingDomains") != []:
        problems.append(f"{label}: NSPrivacyTrackingDomains must be an empty array")
    if manifest.get("NSPrivacyCollectedDataTypes") != []:
        problems.append(
            f"{label}: NSPrivacyCollectedDataTypes must be an empty array (nothing is collected "
            "by the developer; a change here changes the App Privacy answer)"
        )
    declared: dict[str, list[Any]] = {}
    entries = manifest.get("NSPrivacyAccessedAPITypes")
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            reasons = entry.get("NSPrivacyAccessedAPITypeReasons")
            declared[str(entry.get("NSPrivacyAccessedAPIType"))] = (
                reasons if isinstance(reasons, list) else []
            )
    for category in sorted(used - set(declared)):
        problems.append(f"{label}: uses {category} but does not declare it (ITMS-91053)")
    for category, reasons in sorted(declared.items()):
        if category not in REQUIRED_REASON_APIS:
            problems.append(f"{label}: {category} is not a required-reason API category")
        elif category not in used:
            problems.append(f"{label}: declares {category} but no source uses it")
        else:
            allowed = REQUIRED_REASON_APIS[category][1]
            unknown = sorted(str(r) for r in reasons if r not in allowed)
            if not reasons or unknown:
                problems.append(
                    f"{label}: {category} needs approved reason codes, found {reasons} "
                    f"(not approved: {unknown})"
                )
    return problems


def check_privacy_manifests(ios_root: Path) -> list[str]:
    """Every target that uses a required-reason API in its sources has a manifest declaring it
    (and a manifest that exists is checked either way). ``ios_root`` is apps/mobile/ios."""
    problems = []
    for target in TARGETS:
        used = used_required_reason_apis(ios_root / source for source in target.sources)
        path = ios_root / target.folder / MANIFEST
        if not path.is_file():
            if used:
                problems.append(
                    f"{target.name}: uses {sorted(used)} but {target.folder}/{MANIFEST} is "
                    "missing (ITMS-91053)"
                )
            continue
        try:
            manifest = _load_plist(path)
        except (plistlib.InvalidFileException, ValueError, CheckError) as exc:
            problems.append(f"{target.name}/{MANIFEST} is not a valid plist: {exc}")
            continue
        problems += check_privacy_manifest(manifest, used, f"{target.name}/{MANIFEST}")
    return problems


def check_built_privacy_manifests(app: Path, ios_root: Path) -> list[str]:
    """The built App.app and its embedded bundles carry the manifest of every target whose
    sources need one: a manifest in the app does not cover an extension."""
    problems = []
    for target in TARGETS:
        if not used_required_reason_apis(ios_root / source for source in target.sources):
            continue
        if not (app / target.built / MANIFEST).is_file():
            problems.append(
                f"{target.built or app.name}/{MANIFEST} is missing: the bundle uses a "
                "required-reason API and App Store Connect rejects the upload (ITMS-91053)"
            )
    return problems


def check_source(ios: Path = IOS, release_entitlements: Path = RELEASE_ENTITLEMENTS) -> None:
    """Every static check of the app shell; raises CheckError with all problems."""
    problems = check_info_plist(_load_plist(ios / "App" / "Info.plist"))
    problems += check_icons(ios / "App" / "Assets.xcassets" / "AppIcon.appiconset")
    problems += check_entitlements(
        _load_plist(ios / "App" / "App.entitlements"), _load_plist(release_entitlements)
    )
    pbxproj = (ios / "App.xcodeproj" / "project.pbxproj").read_text("utf-8")
    problems += check_project(pbxproj)
    problems += check_localised_info(_load_plist(ios / "App" / "Info.plist"), ios / "App", pbxproj)
    problems += check_project_integrity(pbxproj)
    problems += check_project_files(pbxproj, ios)
    problems += check_privacy_manifests(ios.parent)
    extension = ios / EXTENSION_NAME
    problems += check_extension_plist(_load_plist(extension / "Info.plist"))
    widgets = ios.parent / WIDGETS_NAME
    problems += check_widgets_plist(_load_plist(widgets / "Info.plist"))
    problems += check_widget_entitlements(
        _load_plist(ios / "App" / "App.entitlements"),
        _load_plist(release_entitlements.parent / "widgets.plist"),
    )
    for name in ("MowDeckWidgets.swift", "Provider.swift", "MowingLiveActivity.swift"):
        if not (widgets / name).is_file():
            problems.append(f"{WIDGETS_NAME}/{name} is missing")
    watch = ios.parent / WATCH_NAME
    watch_widgets = ios.parent / WATCH_WIDGETS_NAME
    problems += check_watch_plist(_load_plist(watch / "Info.plist"))
    problems += check_widgets_plist(_load_plist(watch_widgets / "Info.plist"), WATCH_WIDGETS_NAME)
    problems += check_icons(watch / "Assets.xcassets" / "AppIcon.appiconset")
    problems += check_watch_entitlements(
        {
            "MowDeckWatch.entitlements": _load_plist(watch / f"{WATCH_NAME}.entitlements"),
            "MowDeckWatchWidgets.entitlements": _load_plist(
                watch_widgets / f"{WATCH_WIDGETS_NAME}.entitlements"
            ),
            "watch.plist": _load_plist(release_entitlements.parent / "watch.plist"),
            "watch-widgets.plist": _load_plist(release_entitlements.parent / "watch-widgets.plist"),
        }
    )
    for source in (watch / "MowDeckWatchApp.swift", watch_widgets / "MowDeckWatchWidgets.swift"):
        if not source.is_file():
            problems.append(f"{source.name} is missing")
    if (_load_plist(ios / "App" / "Info.plist")).get("NSSupportsLiveActivities") is not True:
        problems.append("App/Info.plist: NSSupportsLiveActivities must be true")
    for source in (extension / "NotificationService.swift",):
        if not source.is_file():
            problems.append(f"{source.name} is missing")
    if problems:
        raise CheckError("\n".join(problems))


def app_icon_names(info: dict[str, Any]) -> set[str]:
    """Icon names the compiled Info.plist declares. actool writes the iOS app icon into
    CFBundleIcons(~ipad) -> CFBundlePrimaryIcon -> CFBundleIconName; a top-level
    CFBundleIconName counts as well."""
    names: set[str] = set()
    top = info.get("CFBundleIconName")
    if isinstance(top, str):
        names.add(top)
    for key in ("CFBundleIcons", "CFBundleIcons~ipad"):
        icons = info.get(key)
        primary = icons.get("CFBundlePrimaryIcon") if isinstance(icons, dict) else None
        name = primary.get("CFBundleIconName") if isinstance(primary, dict) else None
        if isinstance(name, str):
            names.add(name)
    return names


def check_built_extension(appex: Path, version: str, build: str) -> list[str]:
    """The built Notification Service Extension inside App.app/PlugIns."""
    if not (appex / "Info.plist").is_file():
        return [
            f"PlugIns/{appex.name} is missing: the app was built without the notification extension"
        ]
    info = _load_plist(appex / "Info.plist")
    problems = []
    if info.get("CFBundleIdentifier") != EXTENSION_BUNDLE_ID:
        problems.append(f"{appex.name}: CFBundleIdentifier is {info.get('CFBundleIdentifier')!r}")
    if info.get("CFBundleShortVersionString") != version:
        problems.append(
            f"{appex.name}: CFBundleShortVersionString is "
            f"{info.get('CFBundleShortVersionString')!r}"
        )
    if info.get("CFBundleVersion") != build:
        problems.append(
            f"{appex.name}: CFBundleVersion is {info.get('CFBundleVersion')!r}, expected {build!r}"
        )
    extension = info.get("NSExtension")
    point = extension.get("NSExtensionPointIdentifier") if isinstance(extension, dict) else None
    if point != EXTENSION_POINT:
        problems.append(f"{appex.name}: NSExtensionPointIdentifier is {point!r}")
    return problems


def check_built_widgets(appex: Path, version: str, build: str) -> list[str]:
    """The built widget extension inside App.app/PlugIns."""
    if not (appex / "Info.plist").is_file():
        return [f"PlugIns/{appex.name} is missing: the app was built without the widget extension"]
    info = _load_plist(appex / "Info.plist")
    problems = []
    if info.get("CFBundleIdentifier") != WIDGETS_BUNDLE_ID:
        problems.append(f"{appex.name}: CFBundleIdentifier is {info.get('CFBundleIdentifier')!r}")
    if info.get("CFBundleShortVersionString") != version:
        problems.append(
            f"{appex.name}: CFBundleShortVersionString is "
            f"{info.get('CFBundleShortVersionString')!r}"
        )
    if info.get("CFBundleVersion") != build:
        problems.append(
            f"{appex.name}: CFBundleVersion is {info.get('CFBundleVersion')!r}, expected {build!r}"
        )
    extension = info.get("NSExtension")
    point = extension.get("NSExtensionPointIdentifier") if isinstance(extension, dict) else None
    if point != WIDGETS_POINT:
        problems.append(f"{appex.name}: NSExtensionPointIdentifier is {point!r}")
    return problems


def _check_built_bundle(
    bundle: Path, label: str, bundle_id: str, version: str, build: str
) -> tuple[dict[str, Any], list[str]]:
    info = _load_plist(bundle / "Info.plist")
    problems = []
    if info.get("CFBundleIdentifier") != bundle_id:
        problems.append(f"{label}: CFBundleIdentifier is {info.get('CFBundleIdentifier')!r}")
    if info.get("CFBundleShortVersionString") != version:
        problems.append(
            f"{label}: CFBundleShortVersionString is {info.get('CFBundleShortVersionString')!r}"
        )
    if info.get("CFBundleVersion") != build:
        problems.append(
            f"{label}: CFBundleVersion is {info.get('CFBundleVersion')!r}, expected {build!r}"
        )
    return info, problems


def check_built_watch(watch: Path, version: str, build: str) -> list[str]:
    """The built watch app inside App.app/Watch and its complication (ADR 0060)."""
    if not (watch / "Info.plist").is_file():
        return [f"Watch/{watch.name} is missing: the app was built without the watch app"]
    info, problems = _check_built_bundle(watch, watch.name, WATCH_BUNDLE_ID, version, build)
    if info.get("WKCompanionAppBundleIdentifier") != BUNDLE_ID:
        problems.append(
            f"{watch.name}: WKCompanionAppBundleIdentifier is "
            f"{info.get('WKCompanionAppBundleIdentifier')!r}"
        )
    appex = watch / "PlugIns" / f"{WATCH_WIDGETS_NAME}.appex"
    if not (appex / "Info.plist").is_file():
        return [*problems, f"{watch.name}/PlugIns/{appex.name} is missing (the complication)"]
    ext, more = _check_built_bundle(appex, appex.name, WATCH_WIDGETS_BUNDLE_ID, version, build)
    problems += more
    extension = ext.get("NSExtension")
    point = extension.get("NSExtensionPointIdentifier") if isinstance(extension, dict) else None
    if point != WIDGETS_POINT:
        problems.append(f"{appex.name}: NSExtensionPointIdentifier is {point!r}")
    return problems


def check_built_notices(app: Path, source: Path = NOTICES) -> list[str]:
    """The About page and the third-party notices (MIT, Apache-2.0 and OFL require the licence
    text and the copyright notice to ship with the app) are in the bundle, and the notices are
    the committed file that the licence gate (app_notices.py) checks."""
    problems = []
    if not (app / "public" / "about.html").is_file():
        problems.append("the About page is missing (public/about.html)")
    built = app / "public" / "third-party-notices.json"
    if not built.is_file():
        return [*problems, "the third-party notices are missing (public/third-party-notices.json)"]
    try:
        document = json.loads(built.read_text("utf-8"))
        licences = document["licences"]
        packages = [p for entry in licences for p in entry["packages"]]
        texts = [entry["text"] for entry in licences]
    except (ValueError, KeyError, TypeError) as exc:
        return [*problems, f"public/third-party-notices.json is not a notices file: {exc!r}"]
    if not packages or not all(isinstance(t, str) and t.strip() for t in texts):
        problems.append("public/third-party-notices.json lists no packages or has no licence text")
    elif not all(p.get("copyright") for p in packages):
        problems.append("public/third-party-notices.json has a package without a copyright notice")
    if source.is_file() and built.read_bytes() != source.read_bytes():
        problems.append(
            "public/third-party-notices.json differs from apps/mobile/public/"
            "third-party-notices.json: the build did not bundle the committed file"
        )
    return problems


def check_app(
    app: Path, version: str, build: str, ios_root: Path = IOS.parent, notices: Path = NOTICES
) -> None:
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
    icon_names = app_icon_names(info)
    has_car = (app / "Assets.car").is_file()
    if "AppIcon" not in icon_names or not has_car:
        problems.append(
            "no compiled app icon (CFBundleIconName AppIcon and Assets.car): App Store Connect "
            f"rejects the build (ITMS-90713); found icon names {sorted(icon_names) or 'none'}, "
            f"Assets.car {'present' if has_car else 'missing'}, "
            f"Info.plist keys {sorted(k for k in info if 'Icon' in k) or 'none'}"
        )
    problems += check_built_extension(app / "PlugIns" / f"{EXTENSION_NAME}.appex", version, build)
    problems += check_built_widgets(app / "PlugIns" / f"{WIDGETS_NAME}.appex", version, build)
    problems += check_built_watch(app / "Watch" / f"{WATCH_NAME}.app", version, build)
    problems += check_built_privacy_manifests(app, ios_root)
    problems += check_built_notices(app, notices)
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
            print(
                "iOS app shell: Info.plist, icon, entitlements, privacy manifests and project are "
                "App Store ready."
            )
        elif args.command == "app":
            check_app(args.app, args.version, args.build)
            print(
                f"{args.app}: {args.version} ({args.build}), icon catalog, privacy manifests, "
                "third-party notices, no Agora files."
            )
        else:
            print(build_number(args.version, args.rebuild))
    except CheckError as exc:
        for line in str(exc).splitlines():
            print(f"::error::{line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
