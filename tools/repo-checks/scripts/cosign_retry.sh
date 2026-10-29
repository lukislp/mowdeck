# shellcheck shell=bash
# cosign_retry: Sigstore keyless signing for the release workflow that survives Sigstore hiccups.
#
#   . tools/repo-checks/scripts/cosign_retry.sh
#   cosign_sign_blob_verified "$IDENTITY" assets/SHA256SUMS      # writes SHA256SUMS.sigstore.json
#   cosign_sign_verified "$IDENTITY" "$IMAGE@$DIGEST"             # image or chart in the registry
#   cosign_retry cosign attest --yes --predicate p.json --type slsaprovenance "$IMAGE@$DIGEST"
#
# Every function signs, then verifies the result against the workflow identity ($1) and the
# GitHub Actions issuer, and fails the step when that is not possible: nothing is ever skipped
# unsigned. What is retried:
#
#   * HTTP 409 createLogEntryConflict from Rekor. cosign's own HTTP retry re-posts an entry the
#     first request had already written; the run then fails although nothing is wrong. Every
#     attempt is a new cosign process, so it gets a new ephemeral key and a new Fulcio
#     certificate and writes a new transparency log entry; the bundle of the failed attempt is
#     deleted before the next one, so no partial output survives.
#   * TUF and CDN fetches (tuf-repo-cdn.sigstore.dev, the signing config and trusted root),
#     DNS, TCP and TLS errors, timeouts, HTTP 429 and 5xx answers of Fulcio, Rekor, the TSA or
#     the registry.
#
# Anything else (a wrong identity, a missing file, an unknown flag) fails at once with an
# ::error:: line. Waits 15, 30, 60 and 120 seconds between attempts, so at most five attempts
# (COSIGN_RETRY_DELAYS overrides the waits, the tests use zeros; COSIGN overrides the binary).
#
# Idempotent: cosign_sign_blob_verified keeps an existing <file>.sigstore.json that verifies
# for the file and the identity (a rerun of the publish job, a bundle downloaded from the
# release), and cosign_sign_verified does not sign an image again that already verifies.

COSIGN_RETRY_ISSUER="https://token.actions.githubusercontent.com"

# Exit status 0 when the error output in file $1 is a transient Sigstore, network or server
# error worth another attempt.
cosign_retry_transient() {
  grep -qiE \
    'createLogEntryConflict|\[409\]|409 Conflict|an equivalent entry already exists|(^|[^a-z])tuf([^a-z]|$)|signing config|trusted root|dial tcp|i/o timeout|timeout|timed out|deadline exceeded|connection reset|connection refused|broken pipe|unexpected EOF|TLS handshake|no such host|temporary failure in name resolution|server misbehaving|giving up after|too many requests|\[429\]|\[5[0-9][0-9]\]|HTTP 5[0-9][0-9]|status code:? 5[0-9][0-9]|internal server error|bad gateway|service unavailable|gateway timeout' \
    "$1"
}

# cosign_retry_loop DESCRIPTION CLEANUP -- COMMAND...: run COMMAND until it succeeds, retrying on
# transient errors. CLEANUP (a path or "") is deleted before every attempt. Standard output is
# printed only from the successful attempt, standard error of every attempt goes to stderr.
cosign_retry_loop() {
  local what="$1" cleanup="$2"
  shift 3
  local delays="${COSIGN_RETRY_DELAYS:-15 30 60 120}"
  local out err rc attempt=1 delay
  local -a waits=()
  read -r -a waits <<<"$delays"
  out="$(mktemp)"
  err="$(mktemp)"
  while :; do
    [ -z "$cleanup" ] || rm -f -- "$cleanup"
    rc=0
    "$@" >"$out" 2>"$err" || rc=$?
    cat "$err" >&2
    if [ "$rc" = 0 ]; then
      cat "$out"
      rm -f "$out" "$err"
      return 0
    fi
    if [ "$attempt" -gt "${#waits[@]}" ] || ! cosign_retry_transient "$err"; then
      [ -z "$cleanup" ] || rm -f -- "$cleanup"
      echo "::error::cosign failed (exit $rc, attempt $attempt): $what" >&2
      rm -f "$out" "$err"
      return "$rc"
    fi
    delay="${waits[$((attempt - 1))]}"
    echo "::warning::transient Sigstore error (attempt $attempt); retrying in ${delay}s: $what" >&2
    sleep "$delay"
    attempt=$((attempt + 1))
  done
}

# cosign_retry COMMAND...: any cosign command (sign, sign-blob, attest, verify) with the retry.
cosign_retry() {
  [ "$#" -gt 0 ] || { echo "::error::cosign_retry: no command" >&2; return 2; }
  cosign_retry_loop "$*" "" -- "$@"
}

_cosign_bin() { "${COSIGN:-cosign}" "$@"; }

_cosign_verify_blob() { # IDENTITY FILE
  _cosign_bin verify-blob --bundle "$2.sigstore.json" --certificate-identity "$1" \
    --certificate-oidc-issuer "$COSIGN_RETRY_ISSUER" "$2"
}

_cosign_sign_blob_once() { # IDENTITY FILE
  _cosign_bin sign-blob --yes --bundle "$2.sigstore.json" "$2" && _cosign_verify_blob "$1" "$2"
}

# cosign_sign_blob_verified IDENTITY FILE...: a verified <file>.sigstore.json for every FILE.
cosign_sign_blob_verified() {
  local identity="$1" file
  shift
  for file in "$@"; do
    if [ -s "$file.sigstore.json" ] && _cosign_verify_blob "$identity" "$file" >/dev/null 2>&1; then
      echo "$file: existing bundle verifies, kept" >&2
      continue
    fi
    cosign_retry_loop "sign-blob $file" "$file.sigstore.json" -- \
      _cosign_sign_blob_once "$identity" "$file" >/dev/null || return $?
    echo "$file: signed and verified" >&2
  done
}

_cosign_verify_ref() { # IDENTITY REF
  _cosign_bin verify --certificate-identity "$1" --certificate-oidc-issuer "$COSIGN_RETRY_ISSUER" \
    "$2"
}

_cosign_sign_once() { # IDENTITY REF
  _cosign_bin sign --yes "$2" && _cosign_verify_ref "$1" "$2"
}

# cosign_sign_verified IDENTITY REF: a verified signature for an image or chart by digest.
cosign_sign_verified() {
  local identity="$1" ref="$2"
  if _cosign_verify_ref "$identity" "$ref" >/dev/null 2>&1; then
    echo "$ref: already signed by $identity, kept" >&2
    return 0
  fi
  cosign_retry_loop "sign $ref" "" -- _cosign_sign_once "$identity" "$ref" >/dev/null || return $?
  echo "$ref: signed and verified" >&2
}
