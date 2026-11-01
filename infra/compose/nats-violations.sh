#!/bin/sh
# Fails when the NATS server of a running stack refused a client (ADR 0074): a permission
# violation (a service used a subject outside its allow list in nats/auth.conf) or an
# authentication failure. CI runs it after the end-to-end suites; locally, after any test run:
#
#   sh infra/compose/nats-violations.sh [docker compose options, e.g. -p <project> -f <file>]
#
# Prints the offending log lines (subjects and user names, never a password).
set -eu

log="$(docker compose "$@" logs --no-color --no-log-prefix nats)"
found="$(printf '%s\n' "$log" | grep -E 'Publish Violation|Subscription Violation|Authorization Violation|Permissions Violation' || true)"
if [ -n "$found" ]; then
  printf '%s\n' "$found"
  echo "nats-violations: the NATS server refused a client; extend the subject map in" \
    "tools/repo-checks/scripts/nats_auth.py only if the service really needs it (ADR 0074)" >&2
  exit 1
fi
echo "nats-violations: no NATS permission violations"
