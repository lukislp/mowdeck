#!/usr/bin/env bash
# The NATS passwords of the stack (ADR 0073): one per service user of nats/auth.conf, stored as
# NATS_PASSWORD_<USER> in an env file (.env of the stack, demo/demo.env of the public demo).
#
#   ./nats-passwords.sh <env-file>           # adds every missing password, replaces placeholders
#   ./nats-passwords.sh --check <env-file>   # only checks; exit 1 and the names when one is bad
#
# A password is bad when it is missing, empty, a placeholder (change-me..., ...dev-only...) or
# not 24 to 128 letters, digits, '-' and '_' starting with a letter (the NATS server reads the
# value from its environment as a configuration value: a password starting with a digit would be
# taken for a number). Generated passwords are "n" and 48 random hex digits. Existing good values
# are kept, so the script is safe to run again. It never prints a value, only variable names.
#
# After a change, `docker compose up -d` recreates NATS and the services with the new values.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
conf="$here/nats/auth.conf"
check=0

die() {
  echo "nats-passwords: $*" >&2
  exit 1
}

usage() {
  echo "usage: $0 [--check] <env-file>" >&2
  exit 2
}

case "${1:-}" in
  --check)
    check=1
    shift
    ;;
  -h | --help | '') usage ;;
esac
[ "$#" -eq 1 ] || usage
env_file="$1"
[ -f "$conf" ] || die "$conf is missing"
[ -f "$env_file" ] || die "$env_file does not exist"

names="$(grep -o '\$NATS_PASSWORD_[A-Z0-9_]*' "$conf" | tr -d '$' | sort -u | tr '\n' ' ')"
[ -n "${names// /}" ] || die "no NATS_PASSWORD_* variable in $conf"

# The names whose last assignment in the env file is not a good password (one awk run).
# shellcheck disable=SC2016 # the awk program is single quoted on purpose
bad="$(awk -v names="$names" '
  function strip(v) {
    sub(/^[ \t]*["\047]?/, "", v); sub(/["\047]?[ \t\r]*$/, "", v); return v
  }
  {
    line = $0
    sub(/^[ \t]*(export[ \t]+)?/, "", line)
    eq = index(line, "=")
    if (eq > 1) value[substr(line, 1, eq - 1)] = strip(substr(line, eq + 1))
  }
  END {
    n = split(names, list, " ")
    for (i = 1; i <= n; i++) {
      v = value[list[i]]
      ok = length(v) >= 24 && length(v) <= 128 && v ~ /^[A-Za-z][A-Za-z0-9_-]*$/
      if (v ~ /^change-me/ || v ~ /dev-only/) ok = 0
      if (!ok) printf "%s ", list[i]
    }
  }
' "$env_file")"
bad="${bad% }"

if [ "$check" -eq 1 ]; then
  if [ -n "$bad" ]; then
    echo "nats-passwords: $env_file lacks a good value for: $bad" >&2
    echo "nats-passwords: run $here/nats-passwords.sh $env_file (docs/ops/compose.md)" >&2
    exit 1
  fi
  echo "nats-passwords: $env_file has every NATS password"
  exit 0
fi

if [ -z "$bad" ]; then
  echo "nats-passwords: $env_file has every NATS password, nothing changed"
  exit 0
fi

count="$(wc -w <<<"$bad" | tr -d ' ')"
if command -v openssl >/dev/null 2>&1; then
  hex="$(openssl rand -hex "$((24 * count))")"
else
  hex="$(od -An -tx1 -N "$((24 * count))" /dev/urandom | tr -d ' \n')"
fi
[ "${#hex}" -eq "$((48 * count))" ] || die "could not read enough random bytes"

# Rewrite through a private temporary file in the same directory and copy it back with cat, so
# the env file keeps its owner and mode: a bad assignment is replaced in place, a missing one is
# appended. The random hex goes to awk on stdin, never on a command line.
umask 077
tmp="$(mktemp "$env_file.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
# shellcheck disable=SC2016 # the awk program is single quoted on purpose
printf '%s\n' "$hex" | awk -v names="$bad" '
  BEGIN {
    getline hex < "-"
    n = split(names, list, " ")
    for (i = 1; i <= n; i++) { password[list[i]] = "n" substr(hex, (i - 1) * 48 + 1, 48) }
  }
  {
    line = $0
    sub(/^[ \t]*(export[ \t]+)?/, "", line)
    eq = index(line, "=")
    key = eq > 1 ? substr(line, 1, eq - 1) : ""
    if (key in password) {
      if (!(key in done)) print key "=" password[key]
      done[key] = 1
      next
    }
    print
  }
  END {
    for (i = 1; i <= n; i++) if (!(list[i] in done)) print list[i] "=" password[list[i]]
  }
' "$env_file" >"$tmp"
cat "$tmp" >"$env_file"
echo "nats-passwords: set $bad in $env_file"
echo "nats-passwords: apply with docker compose up -d (NATS and the services restart)"
