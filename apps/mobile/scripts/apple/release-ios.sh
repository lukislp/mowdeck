#!/bin/bash
# Builds the iOS app shell for TestFlight and signs it for the App Store: bundled pages and
# `cap sync ios` (common.sh), an unsigned `xcodebuild build`, then codesign with the Apple
# Distribution identity, the App Store profile and apps/mobile/scripts/apple/entitlements/
# app-store.plist (production APNs). Runs in the TestFlight job after keychain.sh setup; the
# Mac path for direct installs (build-ios.sh, sign-and-install.sh, automatic development
# signing) stays as it is.
#
# Signing is done here rather than by xcodebuild's manual signing: build settings given on the
# command line apply to every target, including the Swift packages, which do not take a
# provisioning profile. Building unsigned and signing the bundle inside out avoids that and
# makes every entitlement explicit and checked against the profile.
#
# Environment:
#   APP_VERSION      x.y.z -> CFBundleShortVersionString
#   BUILD_NUMBER     CFBundleVersion (ios_release_check.py build-number)
#   IPA_OUTPUT       output path (default apps/mobile/build/MowDeck-ios.ipa)
#   from keychain.sh setup: APPLE_TEAM_ID, SIGNING_KEYCHAIN, DISTRIBUTION_IDENTITY, PROFILE_DIR
# shellcheck source=../common.sh
. "$(dirname "$0")/../common.sh"

fail() {
    echo "::error::$*" >&2
    exit 1
}

: "${APP_VERSION:?APP_VERSION is not set}" "${BUILD_NUMBER:?BUILD_NUMBER is not set}"
: "${APPLE_TEAM_ID:?}" "${SIGNING_KEYCHAIN:?}" "${DISTRIBUTION_IDENTITY:?}" "${PROFILE_DIR:?}"
IPA_OUTPUT="${IPA_OUTPUT:-$BUILD_DIR/MowDeck-ios.ipa}"
CHECKS="python3 $REPO_DIR/tools/repo-checks/scripts/ios_release_check.py"
SIGNING="python3 $REPO_DIR/tools/repo-checks/scripts/ios_signing.py"
DECLARED="$MOBILE_DIR/scripts/apple/entitlements/app-store.plist"
EXT_BUNDLE_ID="$BUNDLE_ID.NotificationService"
EXT_DECLARED="$MOBILE_DIR/scripts/apple/entitlements/notification-service.plist"
DERIVED="$BUILD_DIR/derived-appstore"

require_mac
echo "MowDeck iOS for TestFlight: version $APP_VERSION, build $BUILD_NUMBER, team $APPLE_TEAM_ID"

# --- 1. Everything that can fail without a build: project, icon, profile, entitlements ---
$CHECKS source
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PROFILE="$($SIGNING find-profile "$PROFILE_DIR" --bundle-id "$BUNDLE_ID" --team "$APPLE_TEAM_ID")"
$SIGNING check-profile "$PROFILE" --bundle-id "$BUNDLE_ID" --team "$APPLE_TEAM_ID"
$SIGNING entitlements --declared "$DECLARED" --profile "$PROFILE" --out "$WORK/entitlements.plist"
# The Notification Service Extension (camera photo in error and theft pushes, ADR 0056) is signed
# with its own App Store profile, for app.mowdeck.mobile.NotificationService.
EXT_PROFILE="$($SIGNING find-profile "$PROFILE_DIR" --bundle-id "$EXT_BUNDLE_ID" --team "$APPLE_TEAM_ID")"
$SIGNING check-profile "$EXT_PROFILE" --bundle-id "$EXT_BUNDLE_ID" --team "$APPLE_TEAM_ID"
$SIGNING entitlements --declared "$EXT_DECLARED" --profile "$EXT_PROFILE" --out "$WORK/ext-entitlements.plist"

# --- 2. Bundled pages, cap sync, unsigned Release build for devices ---
sync_platform ios
rm -rf "$DERIVED"
xcodebuild -project "$XCODE_PROJECT" -scheme App -configuration Release \
    -destination "generic/platform=iOS" \
    -derivedDataPath "$DERIVED" \
    DEVELOPMENT_TEAM="$APPLE_TEAM_ID" CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO \
    MARKETING_VERSION="$APP_VERSION" CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
    build -quiet
BUILT="$DERIVED/Build/Products/Release-iphoneos/App.app"
[ -d "$BUILT" ] || fail "xcodebuild produced no $BUILT"

mkdir -p "$WORK/Payload"
cp -R "$BUILT" "$WORK/Payload/App.app"
APP="$WORK/Payload/App.app"
$CHECKS app "$APP" --version "$APP_VERSION" --build "$BUILD_NUMBER"

# --- 3. Sign inside out: embedded frameworks, then the app with profile and entitlements ---
cp "$PROFILE" "$APP/embedded.mobileprovision"
if [ -d "$APP/Frameworks" ]; then
    find "$APP/Frameworks" -maxdepth 1 \( -name "*.framework" -o -name "*.dylib" \) -print0 \
        | while IFS= read -r -d '' f; do
            codesign -f -s "$DISTRIBUTION_IDENTITY" --keychain "$SIGNING_KEYCHAIN" "$f"
            echo "Signed $(basename "$f")"
        done
fi
EXT="$APP/PlugIns/NotificationService.appex"
[ -d "$EXT" ] || fail "the app has no PlugIns/NotificationService.appex"
cp "$EXT_PROFILE" "$EXT/embedded.mobileprovision"
codesign -f -s "$DISTRIBUTION_IDENTITY" --keychain "$SIGNING_KEYCHAIN" \
    --entitlements "$WORK/ext-entitlements.plist" --generate-entitlement-der "$EXT"
codesign --verify --strict --verbose=2 "$EXT"
codesign -f -s "$DISTRIBUTION_IDENTITY" --keychain "$SIGNING_KEYCHAIN" \
    --entitlements "$WORK/entitlements.plist" --generate-entitlement-der "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

codesign -d --entitlements - --xml "$APP" > "$WORK/final.plist" 2> /dev/null
plutil -extract get-task-allow raw -o - "$WORK/final.plist" | grep -qx false \
    || fail "the app is signed with get-task-allow: not an App Store signature"
plutil -extract aps-environment raw -o - "$WORK/final.plist" | grep -qx production \
    || fail "the app is not signed with aps-environment production"
codesign -d --entitlements - --xml "$EXT" > "$WORK/ext-final.plist" 2> /dev/null
plutil -extract get-task-allow raw -o - "$WORK/ext-final.plist" | grep -qx false \
    || fail "the notification extension is signed with get-task-allow: not an App Store signature"
echo "Signed $EXT_BUNDLE_ID"
echo "Signed $BUNDLE_ID with entitlements: $(plutil -convert json -o - "$WORK/final.plist")"

# --- 4. The .ipa ---
mkdir -p "$(dirname "$IPA_OUTPUT")"
rm -f "$IPA_OUTPUT"
IPA_ABS="$(cd "$(dirname "$IPA_OUTPUT")" && pwd)/$(basename "$IPA_OUTPUT")"
(cd "$WORK" && zip -qry "$IPA_ABS" Payload)
echo "IPA: $IPA_ABS ($(du -h "$IPA_ABS" | cut -f1))"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
        echo "### iOS (App Store signature)"
        echo ""
        echo "| | |"
        echo "| --- | --- |"
        echo "| CFBundleShortVersionString | \`$APP_VERSION\` |"
        echo "| CFBundleVersion | \`$BUILD_NUMBER\` |"
        echo "| Profile | \`$($SIGNING profile "$PROFILE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')\` |"
        echo "| IPA SHA-256 | \`$(shasum -a 256 "$IPA_ABS" | cut -d' ' -f1)\` |"
    } >> "$GITHUB_STEP_SUMMARY"
fi
