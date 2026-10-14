#!/bin/bash
# Build the signed iOS app on the Mac: bundled pages, `cap sync ios`, `xcodebuild archive` with
# automatic development signing (the certificate and profile from provision.sh). The web app is
# not bundled; the app loads it from the MowDeck server (ADR 0036), so a server update needs no
# new build. Result: apps/mobile/build/MowDeck.xcarchive/Products/Applications/App.app
#
#   bash apps/mobile/scripts/build-ios.sh
. "$(dirname "$0")/common.sh"

require_mac
require_team
unlock_keychain
sync_platform ios

VERSION=$(app_version)
BUILD=$(build_number)
echo "Building MowDeck $VERSION ($BUILD) for team $MOWDECK_APPLE_TEAM_ID"

rm -rf "$ARCHIVE"
xcodebuild -project "$XCODE_PROJECT" -scheme App -configuration Release \
    -destination "generic/platform=iOS" \
    -archivePath "$ARCHIVE" \
    -derivedDataPath "$BUILD_DIR/derived" \
    -allowProvisioningUpdates \
    DEVELOPMENT_TEAM="$MOWDECK_APPLE_TEAM_ID" CODE_SIGN_STYLE=Automatic \
    MARKETING_VERSION="$VERSION" CURRENT_PROJECT_VERSION="$BUILD" \
    archive | tail -5

[ -d "$APP_PATH" ] || die "the archive has no App.app ($APP_PATH)"
codesign --verify --strict "$APP_PATH"
echo "Built: $APP_PATH"
echo "Next: bash apps/mobile/scripts/sign-and-install.sh"
