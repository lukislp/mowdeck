#!/bin/bash
# Uploads the signed .ipa to App Store Connect with the API key; the build then shows up in
# TestFlight after Apple's processing. altool is Apple's documented command-line upload path
# (Xcode). Its exit code is not reliable on current Xcode releases, so the JSON result decides.
#
#   bash apps/mobile/scripts/apple/upload-testflight.sh <file.ipa>
#
# Environment: ASC_API_KEY_ID, ASC_API_ISSUER_ID, API_PRIVATE_KEYS_DIR (keychain.sh setup puts
# AuthKey_<id>.p8 there; altool looks the key up by id in that directory).
# Optional: UPLOAD_TIMEOUT_SECONDS (default 1800), ALTOOL_VERBOSE=1 (adds --verbose).
#
# altool retries a blocked or stalled connection silently for a very long time (with the upload
# host *.object-storage.apple.com blocked by the egress policy it stays silent until the job is
# cancelled). Hence no stdin, live output and a hard time limit.
set -euo pipefail

file="${1:?file}"
: "${ASC_API_KEY_ID:?}" "${ASC_API_ISSUER_ID:?}" "${API_PRIVATE_KEYS_DIR:?}"
[ -f "$file" ] || {
    echo "::error::$file does not exist"
    exit 1
}
[ -f "$API_PRIVATE_KEYS_DIR/AuthKey_$ASC_API_KEY_ID.p8" ] || {
    echo "::error::API key file missing"
    exit 1
}

timeout_s="${UPLOAD_TIMEOUT_SECONDS:-1800}"
[[ "$timeout_s" =~ ^[1-9][0-9]*$ ]] || {
    echo "::error::UPLOAD_TIMEOUT_SECONDS must be a positive number"
    exit 1
}
verbose=()
if [ "${ALTOOL_VERBOSE:-}" = 1 ]; then verbose=(--verbose); fi

out="$(mktemp)"
err="$(mktemp)"
trap 'rm -f "$out" "$err"' EXIT
echo "Uploading $file ($(du -h "$file" | cut -f1)) with altool, time limit ${timeout_s}s ..."
start=$SECONDS
set +e
# macOS has no GNU timeout: perl sets an alarm that survives exec, so SIGALRM ends altool itself
# (xcrun execs the tool). Exit code 142 = 128 + SIGALRM. stdin from /dev/null so nothing can wait
# for input; tee shows the output live. stdout (the JSON result) and stderr (log lines) go to
# separate files, so log lines cannot land in the middle of the JSON.
perl -e 'alarm shift @ARGV; exec @ARGV or die "exec failed: $!\n"' "$timeout_s" \
    xcrun altool --upload-app -f "$file" -t ios \
    --apiKey "$ASC_API_KEY_ID" --apiIssuer "$ASC_API_ISSUER_ID" --output-format json \
    "${verbose[@]+"${verbose[@]}"}" < /dev/null 2> >(tee "$err" >&2) | tee "$out"
code=${PIPESTATUS[0]}
set -e
echo "altool finished after $((SECONDS - start))s with exit code $code"
if [ "$code" -eq 142 ]; then
    echo "::error::altool did not finish within ${timeout_s}s and was stopped. A silent hang usually" \
        "means blocked network egress: check the Harden Runner insights of this job for dropped" \
        "Apple hosts (allowed-endpoints in apple-testflight.yml), or re-run with ALTOOL_VERBOSE=1."
    exit 1
fi

python3 - "$out" "$err" "$code" << 'PY'
import json, re, sys
texts = [open(f, encoding="utf-8", errors="replace").read() for f in sys.argv[1:3]]
code = int(sys.argv[3])
result = {}
for text in texts:
    # The pretty-printed result object: "{" and "}" at the start of a line (log lines, e.g. with
    # --verbose, may contain braces of their own).
    match = re.search(r"^\{.*^\}", text, re.S | re.M)
    if match:
        try:
            result = json.loads(match.group(0))
            break
        except ValueError:
            pass
if not result:
    # Unparseable: at least surface Apple's messages as annotations.
    for text in texts:
        for msg in sorted(set(re.findall(r'^\s*"message" : "(.*)",?$', text, re.M))):
            print(f"::error::App Store Connect: {msg}")
errors = result.get("product-errors") or []
for e in errors:
    msg = e.get("message", "")
    reason = (e.get("user-info") or e.get("userInfo") or {}).get("NSLocalizedFailureReason", "")
    print(f"::error::App Store Connect: {msg} {reason}".strip())
if code != 0 or errors or "success-message" not in result:
    if not errors:
        print(f"::error::altool upload failed (exit code {code}); see the output above")
    # The build number comes from the release tag, so a second upload of the same tag is
    # rejected as a duplicate (docs/ops/mobile-apps.md, "TestFlight").
    print("::notice::If App Store Connect rejected the build number as already used, this tag "
          "was uploaded before: run 'Apple TestFlight' again with 'rebuild' set to 1 (then 2, ...).")
    sys.exit(1)
print(result["success-message"])
PY
