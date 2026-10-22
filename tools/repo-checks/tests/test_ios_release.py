"""TestFlight delivery of the iOS app shell (.github/workflows/apple-testflight.yml).

The macOS job runs only on demand, so everything that can be checked without a Mac is checked
here on every pull request: the App Store readiness of the Xcode project (ios_release_check.py),
the profile and entitlement logic of the signing step (ios_signing.py) and the workflow's
guard rails. The profiles are synthetic: a plist wrapped in junk bytes, the shape of the CMS
envelope of a real profile as far as ios_signing.py is concerned.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import plistlib
import re
import struct
import sys
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

TEAM = "ABCDE12345"
BUNDLE = "app.mowdeck.mobile"
FUTURE = dt.datetime(2099, 1, 1)
NOW = dt.datetime(2026, 10, 22)


def _load(repo_root: Path, name: str) -> ModuleType:
    path = repo_root / "tools" / "repo-checks" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check(repo_root: Path) -> ModuleType:
    """ios_release_check.py as a module."""
    return _load(repo_root, "ios_release_check")


@pytest.fixture(scope="module")
def signing(repo_root: Path) -> ModuleType:
    """ios_signing.py as a module."""
    return _load(repo_root, "ios_signing")


def _png(color_type: int, size: int = 1024, trns: bool = False) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, color_type, 0, 0, 0)
    extra = chunk(b"tRNS", b"\x00\x00") if trns else b""
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + extra
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND", b"")
    )


def _profile(**overrides: Any) -> dict[str, Any]:
    ents: dict[str, Any] = {
        "application-identifier": f"{TEAM}.{BUNDLE}",
        "com.apple.developer.team-identifier": TEAM,
        "get-task-allow": False,
        "beta-reports-active": True,
        "keychain-access-groups": [f"{TEAM}.*"],
        "aps-environment": "production",
        "com.apple.developer.usernotifications.time-sensitive": True,
    }
    ents.update(overrides.pop("entitlements", {}))
    profile: dict[str, Any] = {
        "Name": "MowDeck AppStore",
        "UUID": "u-1",
        "TeamIdentifier": [TEAM],
        "ExpirationDate": FUTURE,
        "Platform": ["iOS"],
        "Entitlements": ents,
    }
    profile.update(overrides)
    return profile


def _write_profile(directory: Path, name: str, profile: dict[str, Any]) -> Path:
    path = directory / name
    path.write_bytes(b"\x30\x82junk-before" + plistlib.dumps(profile) + b"junk-after\x00")
    return path


# --- ios_release_check.py ------------------------------------------------------------------


def test_repository_shell_is_app_store_ready(check: ModuleType) -> None:
    check.check_source()


@pytest.mark.parametrize(
    ("version", "rebuild", "expected"),
    [
        ("0.3.12", "", "312"),
        ("1.2.3", "", "10203"),
        ("0.4.0", "1", "400.1"),
        ("12.99.99", "99", "129999.99"),
    ],
)
def test_build_number(check: ModuleType, version: str, rebuild: str, expected: str) -> None:
    assert check.build_number(version, rebuild) == expected


@pytest.mark.parametrize(
    ("version", "rebuild"),
    [
        ("0.3.100", ""),
        ("1.2", ""),
        ("v1.2.3", ""),
        ("1.2.3", "0"),
        ("1.2.3", "100"),
        ("1.2.3", "x"),
    ],
)
def test_build_number_rejects(check: ModuleType, version: str, rebuild: str) -> None:
    with pytest.raises(check.CheckError):
        check.build_number(version, rebuild)


def test_build_number_matches_android_version_code(repo_root: Path) -> None:
    gradle = (repo_root / "apps/mobile/android/app/build.gradle").read_text("utf-8")
    assert re.search(
        r"group\(1\)\.toInteger\(\) \* 10000 \+ versionParts\.group\(2\)\.toInteger\(\) \* 100",
        gradle,
    )


@pytest.mark.parametrize(
    ("color_type", "trns", "alpha"),
    [(2, False, False), (6, False, True), (4, False, True), (2, True, True)],
)
def test_png_alpha(
    check: ModuleType, tmp_path: Path, color_type: int, trns: bool, alpha: bool
) -> None:
    path = tmp_path / "icon.png"
    path.write_bytes(_png(color_type, trns=trns))
    assert check.png_info(path) == (1024, 1024, alpha)


def test_icon_with_alpha_or_wrong_size_rejected(check: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "Contents.json").write_text(
        '{"images": [{"idiom": "universal", "size": "1024x1024", "filename": "i.png"}]}',
        "utf-8",
    )
    (tmp_path / "i.png").write_bytes(_png(6, size=512))
    problems = check.check_icons(tmp_path)
    assert any("512x512" in p for p in problems)
    assert any("alpha" in p for p in problems)
    (tmp_path / "Contents.json").write_text('{"images": []}', "utf-8")
    assert check.check_icons(tmp_path) == ["AppIcon has no 1024x1024 image (the App Store icon)"]


def test_info_plist_problems(check: ModuleType, repo_root: Path) -> None:
    info = plistlib.loads((repo_root / "apps/mobile/ios/App/App/Info.plist").read_bytes())
    assert check.check_info_plist(info) == []
    del info["ITSAppUsesNonExemptEncryption"]
    info["UISupportedInterfaceOrientations~ipad"] = ["UIInterfaceOrientationPortrait"]
    problems = check.check_info_plist(info)
    assert any("ITSAppUsesNonExemptEncryption" in p for p in problems)
    assert any("ITMS-90474" in p for p in problems)
    info["UIRequiresFullScreen"] = True
    assert not any("ITMS-90474" in p for p in check.check_info_plist(info))


def test_entitlements_must_mirror_the_development_build(check: ModuleType) -> None:
    dev = {
        "aps-environment": "development",
        "com.apple.developer.usernotifications.time-sensitive": True,
    }
    assert check.check_entitlements(dev, {**dev, "aps-environment": "production"}) == []
    problems = check.check_entitlements({**dev, "com.apple.developer.healthkit": True}, dev)
    assert any("differ from App.entitlements" in p for p in problems)
    assert any("production" in p for p in problems)


def test_project_settings(check: ModuleType) -> None:
    good = f'PRODUCT_BUNDLE_IDENTIFIER = {BUNDLE};\nTARGETED_DEVICE_FAMILY = "1,2";\n'
    assert check.check_project(good) == []
    bad = good.replace('"1,2"', "1") + "SUPPORTS_MACCATALYST = YES;\n"
    problems = check.check_project(bad)
    assert len(problems) == 2


def _built_app(tmp_path: Path, **info: Any) -> Path:
    app = tmp_path / "App.app"
    (app / "public").mkdir(parents=True)
    (app / "public" / "index.html").write_text("<html></html>", "utf-8")
    (app / "Assets.car").write_bytes(b"car")
    plist = {
        "CFBundleIdentifier": BUNDLE,
        "CFBundleShortVersionString": "0.3.12",
        "CFBundleVersion": "312",
        "ITSAppUsesNonExemptEncryption": False,
        "CFBundleIconName": "AppIcon",
    }
    plist.update(info)
    plist = {key: value for key, value in plist.items() if value is not None}
    (app / "Info.plist").write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_BINARY))
    return app


def test_built_app_passes(check: ModuleType, tmp_path: Path) -> None:
    check.check_app(_built_app(tmp_path), "0.3.12", "312")


def test_built_app_nested_icon_name_passes(check: ModuleType, tmp_path: Path) -> None:
    # What actool really writes for an iOS app: the name only inside CFBundlePrimaryIcon.
    primary = {
        "CFBundlePrimaryIcon": {
            "CFBundleIconFiles": ["AppIcon60x60"],
            "CFBundleIconName": "AppIcon",
        }
    }
    app = _built_app(
        tmp_path, CFBundleIconName=None, CFBundleIcons=primary, **{"CFBundleIcons~ipad": primary}
    )
    check.check_app(app, "0.3.12", "312")


def test_built_app_reports_what_it_found(check: ModuleType, tmp_path: Path) -> None:
    app = _built_app(tmp_path, CFBundleIconName=None)
    (app / "Assets.car").unlink()
    with pytest.raises(check.CheckError) as err:
        check.check_app(app, "0.3.12", "312")
    assert "Assets.car missing" in str(err.value)
    assert "icon names none" in str(err.value)


def test_built_app_problems(check: ModuleType, tmp_path: Path) -> None:
    app = _built_app(tmp_path, CFBundleVersion="1", CFBundleIconName=None)
    (app / "public" / "assets").mkdir()
    (app / "public" / "assets" / "camera.js").write_text("import 'agora-rtc-sdk-ng'", "utf-8")
    (app / "AgoraRtcKit.framework").mkdir()
    with pytest.raises(check.CheckError) as err:
        check.check_app(app, "0.3.12", "312")
    text = str(err.value)
    assert "CFBundleVersion" in text
    assert "ITMS-90713" in text
    assert "camera.js mentions Agora" in text
    assert "AgoraRtcKit.framework: Agora files" in text


# --- ios_signing.py --------------------------------------------------------------------------


def test_profile_summary(signing: ModuleType, tmp_path: Path) -> None:
    path = _write_profile(tmp_path, "a.mobileprovision", _profile())
    info = signing.summary(signing.read_profile(path))
    assert info["bundle_id"] == BUNDLE
    assert info["team"] == TEAM
    assert info["kind"] == "app-store"


def test_not_a_profile(signing: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "x.mobileprovision"
    path.write_bytes(b"nothing")
    with pytest.raises(signing.SigningError):
        signing.read_profile(path)


def test_check_profile_accepts_app_store(signing: ModuleType) -> None:
    assert signing.check_profile(_profile(), "p", BUNDLE, TEAM, now=NOW) == []


def test_check_profile_warns_before_expiry(signing: ModuleType) -> None:
    soon = _profile(ExpirationDate=NOW + dt.timedelta(days=10))
    assert "renew it soon" in signing.check_profile(soon, "p", BUNDLE, TEAM, now=NOW)[0]


@pytest.mark.parametrize(
    ("profile", "fragment"),
    [
        (_profile(entitlements={"get-task-allow": True}, ProvisionedDevices=["x"]), "development"),
        (_profile(entitlements={"get-task-allow": False}, ProvisionedDevices=["x"]), "ad-hoc"),
        (_profile(entitlements={"application-identifier": f"{TEAM}.app.other"}), "app identifier"),
        (_profile(TeamIdentifier=["ZZZZZ99999"]), "team is ZZZZZ99999"),
        (_profile(ExpirationDate=NOW - dt.timedelta(days=1)), "expired"),
        (_profile(Platform=["OSX"]), "expected iOS"),
    ],
)
def test_check_profile_rejects(signing: ModuleType, profile: dict[str, Any], fragment: str) -> None:
    with pytest.raises(signing.SigningError, match=fragment):
        signing.check_profile(profile, "p", BUNDLE, TEAM, now=NOW)


def test_entitlements_merge(signing: ModuleType, repo_root: Path) -> None:
    declared = plistlib.loads(
        (repo_root / "apps/mobile/scripts/apple/entitlements/app-store.plist").read_bytes()
    )
    result = signing.build_entitlements(declared, _profile())
    assert result["aps-environment"] == "production"
    assert result["com.apple.developer.usernotifications.time-sensitive"] is True
    assert result["application-identifier"] == f"{TEAM}.{BUNDLE}"
    assert result["get-task-allow"] is False
    assert result["beta-reports-active"] is True
    assert "keychain-access-groups" not in result


def test_entitlements_missing_capability(signing: ModuleType) -> None:
    profile = _profile()
    del profile["Entitlements"]["com.apple.developer.usernotifications.time-sensitive"]
    with pytest.raises(signing.SigningError, match=r"time-sensitive .*enable the capability"):
        signing.build_entitlements(
            {"com.apple.developer.usernotifications.time-sensitive": True}, profile
        )


def test_entitlements_wrong_aps_environment(signing: ModuleType) -> None:
    profile = _profile(entitlements={"aps-environment": "development"})
    with pytest.raises(signing.SigningError, match="aps-environment"):
        signing.build_entitlements({"aps-environment": "production"}, profile)


def test_entitlements_refuse_development_profile(signing: ModuleType) -> None:
    with pytest.raises(signing.SigningError, match="get-task-allow"):
        signing.build_entitlements({}, _profile(entitlements={"get-task-allow": True}))


def test_find_profile(signing: ModuleType, tmp_path: Path) -> None:
    good = _write_profile(tmp_path, "app.mobileprovision", _profile())
    _write_profile(
        tmp_path,
        "other.mobileprovision",
        _profile(entitlements={"application-identifier": f"{TEAM}.x"}),
    )
    assert signing.find_profile(tmp_path, BUNDLE, TEAM) == good
    _write_profile(tmp_path, "dup.mobileprovision", _profile())
    with pytest.raises(signing.SigningError, match="more than one"):
        signing.find_profile(tmp_path, BUNDLE, TEAM)


# --- the workflow -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow(repo_root: Path) -> dict[Any, Any]:
    """apple-testflight.yml (yaml reads the key "on" as True)."""
    loaded = yaml.safe_load(
        (repo_root / ".github/workflows/apple-testflight.yml").read_text("utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def test_workflow_runs_on_demand_only(workflow: dict[Any, Any]) -> None:
    assert set(workflow[True]) == {"workflow_dispatch"}
    assert set(workflow[True]["workflow_dispatch"]["inputs"]) == {"tag", "rebuild"}
    assert workflow["permissions"] == {"contents": "read"}


def test_workflow_signing_job(workflow: dict[Any, Any]) -> None:
    job = workflow["jobs"]["testflight-ios"]
    assert job["environment"] == "release"
    assert job["permissions"] == {"contents": "read"}
    assert job["needs"] == ["resolve"]
    steps = job["steps"]
    harden = steps[0]
    assert harden["uses"].startswith("step-security/harden-runner@")
    assert "vars.APPLE_RELEASE_EGRESS_POLICY" in harden["with"]["egress-policy"]
    allowed = harden["with"]["allowed-endpoints"].split()
    for host in (
        "*.object-storage.apple.com:443",
        "registry.npmjs.org:443",
        "api.appstoreconnect.apple.com:443",
    ):
        assert host in allowed
    checkout = steps[1]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {
        "ref": "${{ needs.resolve.outputs.sha }}",
        "persist-credentials": False,
    }
    assert steps[-1]["if"] == "always()"
    assert "keychain.sh cleanup" in steps[-1]["run"]


def test_workflow_actions_pinned(repo_root: Path) -> None:
    text = (repo_root / ".github/workflows/apple-testflight.yml").read_text("utf-8")
    uses = re.findall(r"uses: (\S+)(.*)", text)
    assert uses
    for ref, comment in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref
        assert re.fullmatch(r" # v\d+(\.\d+)*", comment), ref
