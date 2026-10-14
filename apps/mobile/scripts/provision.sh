#!/bin/bash
# One-time (and once a year) on the Mac: create the Apple Development certificate, register the
# iPhone and create the development provisioning profile of app.mowdeck.mobile with the Push
# Notifications and Time Sensitive Notifications capabilities (paid developer account).
#
# Xcode does all of it headless through automatic signing: `xcodebuild -allowProvisioningUpdates
# -allowProvisioningDeviceRegistration` builds the app once for the connected iPhone, which
# registers the App ID with the capabilities of ios/App/App/App.entitlements, the device and the
# profile. Prerequisites: the Apple ID of the paid team in Xcode (Xcode -> Settings -> Accounts),
# the iPhone connected by USB once ("Trust this computer") with Developer Mode on
# (Settings -> Privacy & Security -> Developer Mode), MOWDECK_APPLE_TEAM_ID in ~/.mowdeck-ios.env.
#
# Paid profiles and certificates are valid for a year, so there is no 7-day renewal job (that
# is only needed for free Personal Team signing); run this again when the profile expires.
#
#   bash apps/mobile/scripts/provision.sh
. "$(dirname "$0")/common.sh"

require_mac
require_team
unlock_keychain
sync_platform ios

DEVICE=$(xcodebuild -project "$XCODE_PROJECT" -scheme App -showdestinations 2> /dev/null \
    | grep "platform:iOS," | grep -Eo 'id:[0-9A-F]{8}-[0-9A-F]{16}' | head -1 | cut -d: -f2 || true)
[ -n "$DEVICE" ] || die "no iPhone found by Xcode (connect it by USB, unlock it, trust the Mac, enable Developer Mode)"
echo "Device: $DEVICE"
echo "Team:   $MOWDECK_APPLE_TEAM_ID"

xcodebuild -project "$XCODE_PROJECT" -scheme App -configuration Debug \
    -destination "platform=iOS,id=$DEVICE" \
    -derivedDataPath "$BUILD_DIR/provision" \
    -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
    DEVELOPMENT_TEAM="$MOWDECK_APPLE_TEAM_ID" CODE_SIGN_STYLE=Automatic \
    build | tail -5

PROFILE=$(find_profile "$MOWDECK_APPLE_TEAM_ID.$BUNDLE_ID")
[ -n "$PROFILE" ] || die "Xcode created no profile for $MOWDECK_APPLE_TEAM_ID.$BUNDLE_ID (see the build log above)"
PLIST=$(security cms -D -i "$PROFILE")
echo "Profile:      $PROFILE"
echo "Expires:      $(echo "$PLIST" | plutil -extract ExpirationDate raw -o - -)"
echo "APNs:         $(echo "$PLIST" | plutil -extract Entitlements.aps-environment raw -o - - 2> /dev/null || echo 'MISSING - is the team a paid account?')"
echo "Identities:"
security find-identity -v -p codesigning | grep "Apple Development" || die "no Apple Development identity in the keychain"
echo "Done. Next: bash apps/mobile/scripts/build-ios.sh && bash apps/mobile/scripts/sign-and-install.sh"
