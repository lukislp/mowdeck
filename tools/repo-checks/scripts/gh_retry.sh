# shellcheck shell=bash
# gh_retry: run a GitHub CLI (or other GitHub API) command and retry it when GitHub answers with a
# rate limit, so a workflow step waits for the GITHUB_TOKEN quota instead of failing on it.
#
#   . tools/repo-checks/scripts/gh_retry.sh
#   tag="$(gh_retry gh release view --json tagName --jq .tagName)"
#
# Retried: HTTP 403 and 429 answers and any "rate limit" message (primary and secondary). Waits
# 30, 60 and 120 seconds (GH_RETRY_DELAYS overrides them, the tests use zeros), so at most four
# attempts. Anything else fails at once with the command's exit code and an ::error:: line, as
# does the last attempt. Standard output is buffered and printed only once, from the attempt that
# succeeded, so a partial answer of a failed attempt never reaches a pipe or a $(...).
#
# Only for idempotent commands: reads, `gh release upload --clobber`, `gh release edit` with the
# same notes. Never wrap a command that would create something twice when an attempt that
# reported a failure had in fact succeeded.
gh_retry() {
  local delays="${GH_RETRY_DELAYS:-30 60 120}"
  local out err rc=0 attempt=1 delay
  [ "$#" -gt 0 ] || { echo "::error::gh_retry: no command" >&2; return 2; }
  out="$(mktemp)"
  err="$(mktemp)"
  local -a cmd=("$@") waits=()
  read -r -a waits <<<"$delays"
  while :; do
    rc=0
    "${cmd[@]}" >"$out" 2>"$err" || rc=$?
    cat "$err" >&2
    if [ "$rc" = 0 ]; then
      cat "$out"
      rm -f "$out" "$err"
      return 0
    fi
    if [ "$attempt" -gt "${#waits[@]}" ] \
      || ! grep -qiE 'rate limit|HTTP 403|HTTP 429|\(403\)|\(429\)' "$err" "$out"; then
      echo "::error::GitHub API call failed (exit $rc, attempt $attempt): ${cmd[*]}" >&2
      rm -f "$out" "$err"
      return "$rc"
    fi
    delay="${waits[$((attempt - 1))]}"
    echo "::warning::GitHub API rate limit (attempt $attempt); retrying in ${delay}s: ${cmd[*]}" >&2
    sleep "$delay"
    attempt=$((attempt + 1))
  done
}
