#!/bin/bash
# Build the Android APK: bundled pages, `cap sync android`, `gradlew assembleRelease`.
# Prerequisites: Node.js 24 with pnpm, JDK 21, the Android SDK (ANDROID_HOME). Runs on Linux,
# macOS and Git Bash on Windows; the release workflow runs it on ubuntu for every v* tag.
#
# Signing (one keystore for all releases, otherwise an update cannot replace the installed app):
#   MOWDECK_ANDROID_KEYSTORE           path to the .jks, or
#   MOWDECK_ANDROID_KEYSTORE_BASE64    the .jks as base64 (the GitHub secret)
#   MOWDECK_ANDROID_KEYSTORE_PASSWORD, MOWDECK_ANDROID_KEY_ALIAS, MOWDECK_ANDROID_KEY_PASSWORD
# Without a keystore the APK is signed with the debug key (local testing only).
#
#   bash apps/mobile/scripts/build-android.sh     # -> apps/mobile/build/mowdeck-<version>.apk
. "$(dirname "$0")/common.sh"

command -v java > /dev/null || die "java not found: install JDK 21"
[ -n "${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}" ] || die "set ANDROID_HOME to the Android SDK"

VERSION=$(app_version)
export MOWDECK_VERSION="$VERSION"

if [ -z "${MOWDECK_ANDROID_KEYSTORE:-}" ] && [ -n "${MOWDECK_ANDROID_KEYSTORE_BASE64:-}" ]; then
    MOWDECK_ANDROID_KEYSTORE="$(mktemp -d)/release.jks"
    echo "$MOWDECK_ANDROID_KEYSTORE_BASE64" | base64 --decode > "$MOWDECK_ANDROID_KEYSTORE"
    trap 'rm -f "$MOWDECK_ANDROID_KEYSTORE"' EXIT
fi
if [ -n "${MOWDECK_ANDROID_KEYSTORE:-}" ]; then
    export MOWDECK_ANDROID_KEYSTORE
    [ -n "${MOWDECK_ANDROID_KEYSTORE_PASSWORD:-}" ] || die "MOWDECK_ANDROID_KEYSTORE_PASSWORD is missing"
    [ -n "${MOWDECK_ANDROID_KEY_ALIAS:-}" ] || die "MOWDECK_ANDROID_KEY_ALIAS is missing"
    echo "Signing with $MOWDECK_ANDROID_KEYSTORE (alias $MOWDECK_ANDROID_KEY_ALIAS)"
else
    echo "warning: no keystore - the APK is debug-signed (local testing only)" >&2
fi

sync_platform android
(cd "$MOBILE_DIR/android" && bash ./gradlew --no-daemon assembleRelease)

mkdir -p "$BUILD_DIR"
OUT="$BUILD_DIR/mowdeck-$VERSION.apk"
cp "$MOBILE_DIR/android/app/build/outputs/apk/release/app-release.apk" "$OUT"
echo "Built: $OUT"
