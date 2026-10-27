#!/bin/bash
# Signing material for the TestFlight job (.github/workflows/apple-testflight.yml). Everything
# lands in one throwaway directory under RUNNER_TEMP and in a temporary keychain; "cleanup"
# removes both and runs in an always() step. The Mac scripts next door (build-ios.sh and
# friends) keep using automatic development signing from the login keychain; nothing here
# touches it.
#
#   bash apps/mobile/scripts/apple/keychain.sh check     fail if a required secret is empty
#   bash apps/mobile/scripts/apple/keychain.sh setup     keychain + certificate + profile + API key
#   bash apps/mobile/scripts/apple/keychain.sh cleanup
#
# Secrets (GitHub Environment "release", docs/ops/mobile-apps.md "TestFlight"):
#   APPLE_TEAM_ID                       10-character team id
#   APPLE_DISTRIBUTION_P12_BASE64       "Apple Distribution" certificate + private key, .p12, base64
#   APPLE_DISTRIBUTION_P12_PASSWORD
#   APPLE_PROFILE_IOS_BASE64            App Store provisioning profile for app.mowdeck.mobile, base64
#   APPLE_PROFILE_IOS_NOTIFICATION_BASE64  App Store profile for app.mowdeck.mobile.NotificationService (the
#                                       Notification Service Extension that adds the camera photo, ADR 0056), base64
#   APPLE_PROFILE_IOS_WIDGETS_BASE64    App Store profile for app.mowdeck.mobile.Widgets (the widget
#                                       extension with the Live Activity, ADR 0059), base64
#   ASC_API_KEY_P8_BASE64               App Store Connect API key (.p8), base64
#   ASC_API_KEY_ID
#   ASC_API_ISSUER_ID
#
# "setup" appends to GITHUB_ENV: SIGNING_KEYCHAIN, SIGNING_DIR, PROFILE_DIR,
# DISTRIBUTION_IDENTITY (SHA-1 of the imported identity), ASC_API_KEY_PATH and
# API_PRIVATE_KEYS_DIR (where altool looks up AuthKey_<id>.p8).
#
# The .p12 must use the legacy encryption macOS understands: export it from Keychain Access or
# with `openssl pkcs12 -export -legacy ...` (OpenSSL 3).
set -euo pipefail

# Apple's WWDR G3 intermediate certificate, pinned by SHA-256 (https://www.apple.com/
# certificateauthority/, valid to 2030-02-20). A .p12 from Keychain Access carries only the leaf
# certificate; codesign needs the chain.
WWDR_G3_SHA256=dcf21878c77f4198e4b4614f03d696d89c66c66008d4244e1b99161aac91601f

REQUIRED="APPLE_TEAM_ID APPLE_DISTRIBUTION_P12_BASE64 APPLE_DISTRIBUTION_P12_PASSWORD APPLE_PROFILE_IOS_BASE64 APPLE_PROFILE_IOS_NOTIFICATION_BASE64 APPLE_PROFILE_IOS_WIDGETS_BASE64 ASC_API_KEY_P8_BASE64 ASC_API_KEY_ID ASC_API_ISSUER_ID"

fail() {
    echo "::error::$*" >&2
    exit 1
}

check() {
    local missing="" name
    for name in $REQUIRED; do
        [ -n "${!name:-}" ] || missing="$missing $name"
    done
    if [ -n "$missing" ]; then
        fail "Apple signing secrets missing in the 'release' environment:$missing (docs/ops/mobile-apps.md, \"TestFlight\")."
    fi
    [[ "$APPLE_TEAM_ID" =~ ^[A-Z0-9]{10}$ ]] || fail "APPLE_TEAM_ID must be the 10-character team id."
    [[ "$ASC_API_KEY_ID" =~ ^[A-Z0-9]{8,12}$ ]] || fail "ASC_API_KEY_ID does not look like a key id."
    echo "All Apple signing secrets are set."
}

export_env() {
    echo "$1=$2" >> "${GITHUB_ENV:-/dev/null}"
    export "$1=$2"
}

decode() {
    # decode <env var name> <output file>
    local value="${!1}"
    printf '%s' "$value" | base64 --decode > "$2" 2> /dev/null || fail "$1 is not valid base64."
    [ -s "$2" ] || fail "$1 decodes to an empty file."
}

setup() {
    check

    export_env SIGNING_DIR "$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/mowdeck-signing.XXXXXX")"
    export_env PROFILE_DIR "$SIGNING_DIR/profiles"
    mkdir -p "$PROFILE_DIR" "$SIGNING_DIR/private_keys"
    chmod 700 "$SIGNING_DIR"

    export_env SIGNING_KEYCHAIN "$SIGNING_DIR/signing.keychain-db"
    local kc_password
    kc_password="$(openssl rand -hex 24)"
    security create-keychain -p "$kc_password" "$SIGNING_KEYCHAIN"
    security set-keychain-settings -lut 7200 "$SIGNING_KEYCHAIN"
    security unlock-keychain -p "$kc_password" "$SIGNING_KEYCHAIN"
    # First in the search list so codesign finds the chain; the login keychain stays.
    # shellcheck disable=SC2046
    security list-keychains -d user -s "$SIGNING_KEYCHAIN" $(security list-keychains -d user | tr -d '"')

    local ca="$SIGNING_DIR/AppleWWDRCAG3.cer"
    curl -fsSL --retry 3 -o "$ca" "https://www.apple.com/certificateauthority/AppleWWDRCAG3.cer" \
        || fail "download of Apple's WWDR G3 certificate failed"
    [ "$(shasum -a 256 "$ca" | cut -d' ' -f1)" = "$WWDR_G3_SHA256" ] \
        || fail "AppleWWDRCAG3.cer does not match its pinned SHA-256"
    security import "$ca" -k "$SIGNING_KEYCHAIN" > /dev/null

    local p12="$SIGNING_DIR/distribution.p12"
    decode APPLE_DISTRIBUTION_P12_BASE64 "$p12"
    security import "$p12" -k "$SIGNING_KEYCHAIN" -f pkcs12 -P "$APPLE_DISTRIBUTION_P12_PASSWORD" \
        -T /usr/bin/codesign -T /usr/bin/security > /dev/null \
        || fail "APPLE_DISTRIBUTION_P12_BASE64 could not be imported: wrong password, no private key in the .p12, or AES-encrypted (export with 'openssl pkcs12 -export -legacy')?"
    rm -f "$p12"
    # Lets codesign use the key without a UI prompt.
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$kc_password" "$SIGNING_KEYCHAIN" > /dev/null

    local list line
    list="$(security find-identity -v -p codesigning "$SIGNING_KEYCHAIN")"
    line="$(printf '%s\n' "$list" | grep -F '"Apple Distribution: ' | grep -F "($APPLE_TEAM_ID)\"" | head -1 || true)"
    if [ -z "$line" ]; then
        echo "Identities in the signing keychain:" >&2
        printf '%s\n' "$list" >&2
        fail "no valid 'Apple Distribution' identity for team $APPLE_TEAM_ID in APPLE_DISTRIBUTION_P12_BASE64"
    fi
    export_env DISTRIBUTION_IDENTITY "$(printf '%s\n' "$line" | sed -E 's/^ *[0-9]+\) ([0-9A-F]{40}) .*$/\1/')"
    echo "Distribution identity: $(printf '%s\n' "$line" | sed -E 's/^ *[0-9]+\) [0-9A-F]{40} "(.*)"$/\1/')"

    decode APPLE_PROFILE_IOS_BASE64 "$PROFILE_DIR/app.mobileprovision"
    decode APPLE_PROFILE_IOS_NOTIFICATION_BASE64 "$PROFILE_DIR/notification-service.mobileprovision"
    decode APPLE_PROFILE_IOS_WIDGETS_BASE64 "$PROFILE_DIR/widgets.mobileprovision"

    export_env ASC_API_KEY_PATH "$SIGNING_DIR/private_keys/AuthKey_$ASC_API_KEY_ID.p8"
    decode ASC_API_KEY_P8_BASE64 "$ASC_API_KEY_PATH"
    grep -q "BEGIN PRIVATE KEY" "$ASC_API_KEY_PATH" || fail "ASC_API_KEY_P8_BASE64 is not a .p8 private key."
    chmod 600 "$ASC_API_KEY_PATH"
    export_env API_PRIVATE_KEYS_DIR "$SIGNING_DIR/private_keys"
}

cleanup() {
    if [ -n "${SIGNING_KEYCHAIN:-}" ] && [ -f "$SIGNING_KEYCHAIN" ]; then
        security delete-keychain "$SIGNING_KEYCHAIN" || true
    fi
    if [ -n "${SIGNING_DIR:-}" ] && [ -d "$SIGNING_DIR" ]; then
        rm -rf "$SIGNING_DIR"
    fi
    echo "Signing keychain and decoded files removed."
}

case "${1:-}" in
    check) check ;;
    setup) setup ;;
    cleanup) cleanup ;;
    *)
        echo "usage: $0 check|setup|cleanup" >&2
        exit 2
        ;;
esac
