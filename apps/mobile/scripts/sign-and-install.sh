#!/bin/bash
# Install the app built by build-ios.sh on the iPhone, directly from the Mac (no TestFlight, no
# App Store): check the signature and the push entitlement, re-sign with the newest profile when
# asked (MOWDECK_RESIGN=1, for example after provision.sh renewed an expired profile without a
# rebuild) and install with `xcrun devicectl device install app` over USB or Wi-Fi (the iPhone
# must have been paired once by USB). The app's data and login stay on an update.
#
#   bash apps/mobile/scripts/sign-and-install.sh            # the first paired iPhone
#   MOWDECK_DEVICE_ID=<udid> bash apps/mobile/scripts/sign-and-install.sh
. "$(dirname "$0")/common.sh"

require_mac
APP="${1:-$APP_PATH}"
[ -d "$APP" ] || die "no app at $APP - run apps/mobile/scripts/build-ios.sh first"
unlock_keychain

if [ "${MOWDECK_RESIGN:-0}" = "1" ]; then
    require_team
    IDENTITY=$(security find-identity -v -p codesigning | awk -F'"' '/Apple Development/ {print $2; exit}')
    [ -n "$IDENTITY" ] || die "no 'Apple Development' identity in the keychain - run provision.sh"
    PROFILE=$(find_profile "$MOWDECK_APPLE_TEAM_ID.$BUNDLE_ID")
    [ -n "$PROFILE" ] || die "no profile for $MOWDECK_APPLE_TEAM_ID.$BUNDLE_ID - run provision.sh"
    WORK=$(mktemp -d)
    trap 'rm -rf "$WORK"' EXIT
    security cms -D -i "$PROFILE" | plutil -extract Entitlements xml1 -o "$WORK/entitlements.plist" -
    cp "$PROFILE" "$APP/embedded.mobileprovision"
    if [ -d "$APP/Frameworks" ]; then
        find "$APP/Frameworks" -maxdepth 1 \( -name "*.dylib" -o -name "*.framework" \) -print0 \
            | while IFS= read -r -d '' framework; do codesign -f -s "$IDENTITY" "$framework"; done
    fi
    # The notification extension (camera photo in pushes) has its own App ID and profile.
    EXT_PROFILE=$(find_profile "$MOWDECK_APPLE_TEAM_ID.$BUNDLE_ID.NotificationService")
    if [ -d "$APP/PlugIns/NotificationService.appex" ] && [ -n "$EXT_PROFILE" ]; then
        security cms -D -i "$EXT_PROFILE" | plutil -extract Entitlements xml1 -o "$WORK/ext-entitlements.plist" -
        cp "$EXT_PROFILE" "$APP/PlugIns/NotificationService.appex/embedded.mobileprovision"
        codesign -f -s "$IDENTITY" --entitlements "$WORK/ext-entitlements.plist" "$APP/PlugIns/NotificationService.appex"
    fi
    # The widget extension (widgets and the mowing Live Activity, ADR 0059) likewise.
    WIDGETS_PROFILE=$(find_profile "$MOWDECK_APPLE_TEAM_ID.$BUNDLE_ID.Widgets")
    if [ -d "$APP/PlugIns/MowDeckWidgets.appex" ] && [ -n "$WIDGETS_PROFILE" ]; then
        security cms -D -i "$WIDGETS_PROFILE" | plutil -extract Entitlements xml1 -o "$WORK/widgets-entitlements.plist" -
        cp "$WIDGETS_PROFILE" "$APP/PlugIns/MowDeckWidgets.appex/embedded.mobileprovision"
        codesign -f -s "$IDENTITY" --entitlements "$WORK/widgets-entitlements.plist" "$APP/PlugIns/MowDeckWidgets.appex"
    fi
    # The Apple Watch app and its complication (ADR 0060), inside out: complication, watch app.
    WATCH="$APP/Watch/MowDeckWatch.app"
    WATCH_PROFILE=$(find_profile "$MOWDECK_APPLE_TEAM_ID.$BUNDLE_ID.watchkitapp")
    WATCH_WIDGETS_PROFILE=$(find_profile "$MOWDECK_APPLE_TEAM_ID.$BUNDLE_ID.watchkitapp.Widgets")
    if [ -d "$WATCH" ] && [ -n "$WATCH_PROFILE" ] && [ -n "$WATCH_WIDGETS_PROFILE" ]; then
        security cms -D -i "$WATCH_WIDGETS_PROFILE" | plutil -extract Entitlements xml1 -o "$WORK/watch-widgets-entitlements.plist" -
        cp "$WATCH_WIDGETS_PROFILE" "$WATCH/PlugIns/MowDeckWatchWidgets.appex/embedded.mobileprovision"
        codesign -f -s "$IDENTITY" --entitlements "$WORK/watch-widgets-entitlements.plist" "$WATCH/PlugIns/MowDeckWatchWidgets.appex"
        security cms -D -i "$WATCH_PROFILE" | plutil -extract Entitlements xml1 -o "$WORK/watch-entitlements.plist" -
        cp "$WATCH_PROFILE" "$WATCH/embedded.mobileprovision"
        codesign -f -s "$IDENTITY" --entitlements "$WORK/watch-entitlements.plist" "$WATCH"
    fi
    codesign -f -s "$IDENTITY" --entitlements "$WORK/entitlements.plist" "$APP"
    echo "Re-signed with $IDENTITY and $PROFILE"
fi

codesign --verify --deep --strict "$APP" && echo "Signature OK"
APS=$(codesign -d --entitlements - --xml "$APP" 2> /dev/null | plutil -extract aps-environment raw -o - - 2> /dev/null || true)
if [ -n "$APS" ]; then
    echo "APNs environment: $APS (the server needs MOWDECK_APNS_ENVIRONMENT=$APS)"
else
    echo "warning: no aps-environment entitlement - background notifications (APNs) will not work" >&2
fi
EXPIRES=$(security cms -D -i "$APP/embedded.mobileprovision" 2> /dev/null | plutil -extract ExpirationDate raw -o - - 2> /dev/null || true)
[ -n "$EXPIRES" ] && echo "Profile expires: $EXPIRES (run provision.sh and this script again before then)"

DEVICE=$(iphone_id)
[ -n "$DEVICE" ] || die "no iPhone found (paired by USB once, unlocked, on the same Wi-Fi?) - see 'xcrun devicectl list devices'"
echo "Installing on $DEVICE ..."
xcrun devicectl device install app --device "$DEVICE" "$APP"
xcrun devicectl device process launch --device "$DEVICE" "$BUNDLE_ID" > /dev/null 2>&1 || true
echo "Installed. The first start asks for the server address."
