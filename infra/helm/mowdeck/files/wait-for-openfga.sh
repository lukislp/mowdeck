#!/bin/sh
# Init container of the authz-bootstrap job: wait until OpenFGA answers an authenticated request.
# Environment: FGA_API_URL, FGA_API_TOKEN, WAIT_TIMEOUT_SECONDS.
set -u
timeout="${WAIT_TIMEOUT_SECONDS:-900}"
start="$(date +%s)"
until fga store list > /dev/null 2>&1; do
  if [ $(( $(date +%s) - start )) -ge "$timeout" ]; then
    echo "wait-for-openfga: $FGA_API_URL not reachable after ${timeout}s"
    exit 1
  fi
  echo "wait-for-openfga: waiting for $FGA_API_URL"
  sleep 3
done
echo "wait-for-openfga: $FGA_API_URL is up"
