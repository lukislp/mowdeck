#!/bin/bash
# Shared helpers of the MowDeck app shell scripts (sourced, not run). Works the same in a
# Terminal on the Mac and over SSH from the PC.
#
# Optional per-machine settings live in ~/.mowdeck-ios.env (never in the repository):
#   MOWDECK_APPLE_TEAM_ID=ABCDE12345   # Team ID of the paid Apple developer account (required)
#   MOWDECK_DEVICE_ID=00008140-...     # the iPhone (default: the first iPhone devicectl sees)
#   MOWDECK_VERSION=1.2.3              # app version (default: latest v* tag, else 0.1.0)
# The Mac login password for headless keychain access over SSH (chmod 600, stays local):
#   ~/.mowdeck-sign-pass

set -euo pipefail

MOBILE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$MOBILE_DIR/../.." && pwd)"
BUILD_DIR="$MOBILE_DIR/build"
BUNDLE_ID="app.mowdeck.mobile"
XCODE_PROJECT="$MOBILE_DIR/ios/App/App.xcodeproj"
ARCHIVE="$BUILD_DIR/MowDeck.xcarchive"
APP_PATH="$ARCHIVE/Products/Applications/App.app"

# shellcheck disable=SC1090,SC1091
[ -f "$HOME/.mowdeck-ios.env" ] && . "$HOME/.mowdeck-ios.env"

die() {
    echo "error: $*" >&2
    exit 1
}

require_mac() {
    [ "$(uname -s)" = "Darwin" ] || die "this script runs on the Mac (Xcode)"
    command -v xcodebuild > /dev/null || die "xcodebuild not found: install Xcode and run 'sudo xcode-select -s /Applications/Xcode.app'"
}

require_team() {
    [ -n "${MOWDECK_APPLE_TEAM_ID:-}" ] || die "set MOWDECK_APPLE_TEAM_ID (Xcode -> Settings -> Accounts -> your team) in ~/.mowdeck-ios.env"
}

# SSH sessions get their own, locked security session: unlock the login keychain with the
# password from ~/.mowdeck-sign-pass so codesign can use the signing identity.
unlock_keychain() {
    if [ -f "$HOME/.mowdeck-sign-pass" ]; then
        security unlock-keychain -p "$(cat "$HOME/.mowdeck-sign-pass")" "$HOME/Library/Keychains/login.keychain-db"
        security set-keychain-settings -t 3600 -l "$HOME/Library/Keychains/login.keychain-db" || true
        echo "Keychain unlocked."
    elif [ -n "${SSH_CONNECTION:-}" ]; then
        echo "warning: SSH session without ~/.mowdeck-sign-pass; codesign may fail with errSecInternalComponent" >&2
    fi
}

# The app version: MOWDECK_VERSION, else the newest v* tag, else 0.1.0. The build number is the
# commit count, so every install is newer than the last one.
app_version() {
    if [ -n "${MOWDECK_VERSION:-}" ]; then
        echo "${MOWDECK_VERSION#v}"
        return
    fi
    local tag
    tag=$(git -C "$REPO_DIR" describe --tags --abbrev=0 --match 'v*' 2> /dev/null || true)
    tag="${tag#v}"
    echo "${tag%%-*}" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' && echo "${tag%%-*}" || echo "0.1.0"
}

build_number() {
    git -C "$REPO_DIR" rev-list --count HEAD 2> /dev/null || echo 1
}

# Install the app shell's packages and copy the bundled pages and the plugin list into the
# platform projects (`cap sync` also writes the Swift package list and Gradle settings). Only
# `mobile` and the workspace packages it depends on are installed (`--filter mobile...`): the
# shell never needs the web app's dependencies, so the Agora Web SDK of apps/web is not even
# downloaded for an app build (LICENSING.md).
sync_platform() {
    local platform="$1"
    command -v pnpm > /dev/null || die "pnpm not found: install Node.js 24 and run 'corepack enable'"
    (cd "$REPO_DIR" && pnpm install --frozen-lockfile --filter 'mobile...')
    (cd "$MOBILE_DIR" && pnpm build && pnpm exec cap sync "$platform")
}

# The iPhone to use: MOWDECK_DEVICE_ID, else the first paired iPhone (USB or Wi-Fi).
iphone_id() {
    if [ -n "${MOWDECK_DEVICE_ID:-}" ]; then
        echo "$MOWDECK_DEVICE_ID"
        return
    fi
    xcrun devicectl list devices 2> /dev/null | grep -i "iPhone" | grep -v -i "Watch" \
        | grep -Eo '[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}' | head -1 || true
}

# The newest development provisioning profile for TEAM.BUNDLE_ID, if any.
find_profile() {
    local wanted="$1" best="" best_exp="" dir p plist appid exp
    for dir in "$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles" \
        "$HOME/Library/MobileDevice/Provisioning Profiles"; do
        [ -d "$dir" ] || continue
        for p in "$dir"/*.mobileprovision; do
            [ -f "$p" ] || continue
            plist=$(security cms -D -i "$p" 2> /dev/null) || continue
            appid=$(echo "$plist" | plutil -extract Entitlements.application-identifier raw -o - - 2> /dev/null) || continue
            [ "$appid" = "$wanted" ] || continue
            exp=$(echo "$plist" | plutil -extract ExpirationDate raw -o - - 2> /dev/null) || exp=""
            if [ -z "$best" ] || [[ "$exp" > "$best_exp" ]]; then
                best="$p"
                best_exp="$exp"
            fi
        done
    done
    echo "$best"
}
