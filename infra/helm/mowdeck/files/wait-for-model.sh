#!/bin/sh
# Init container (authz-bootstrap image): wait until the latest authorization model of the store
# $FGA_STORE_NAME is the model shipped in this image (/model/model.fga), compared like the
# bootstrap job does (both rendered to DSL by the same CLI transformer). Services pin the latest
# model at startup, so they must not start before the bootstrap of their release wrote it.
# Environment: FGA_API_URL, FGA_API_TOKEN, FGA_STORE_NAME, WAIT_TIMEOUT_SECONDS.
set -u
store_name="${FGA_STORE_NAME:-mowdeck}"
timeout="${WAIT_TIMEOUT_SECONDS:-900}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

fga model transform --file /model/model.fga --output-format json > "$work/wanted.json" || exit 1
fga model transform --file "$work/wanted.json" --output-format fga > "$work/wanted.fga" || exit 1

start="$(date +%s)"
while :; do
  store_id="$(fga store list --name "$store_name" 2>/dev/null \
    | jq -r '.stores // [] | sort_by(.created_at) | .[0].id // empty')"
  if [ -n "$store_id" ] \
    && fga model get --store-id "$store_id" > "$work/current.fga" 2>/dev/null \
    && cmp -s "$work/current.fga" "$work/wanted.fga"; then
    echo "wait-for-model: store $store_name ($store_id) has this release's model"
    exit 0
  fi
  if [ $(( $(date +%s) - start )) -ge "$timeout" ]; then
    echo "wait-for-model: giving up after ${timeout}s (store ${store_id:-missing})"
    exit 1
  fi
  echo "wait-for-model: waiting for the authorization model (store ${store_id:-missing})"
  sleep 3
done
