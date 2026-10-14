#!/bin/sh
# Create the OpenFGA store and write the authorization model, idempotently.
#
#   1. Find the store named $FGA_STORE_NAME (default "mowdeck"); create it if missing.
#      With several stores of that name the oldest wins (same rule as mowdeck-authz).
#   2. Compare the latest model of that store with $FGA_MODEL_FILE. Both sides are
#      rendered to DSL by the same CLI transformer, so comments and formatting do
#      not count as a change. Write the model only when it differs.
#
# Services do not need the ids: mowdeck-authz resolves the store by name and pins
# the latest model at startup. The ids are printed for operators.
#
# Environment: FGA_API_URL, FGA_API_TOKEN (read by the fga CLI), FGA_STORE_NAME,
# FGA_MODEL_FILE.
set -eu

: "${FGA_API_URL:?FGA_API_URL is required}"
: "${FGA_API_TOKEN:?FGA_API_TOKEN is required}"
store_name="${FGA_STORE_NAME:-mowdeck}"
model_file="${FGA_MODEL_FILE:-/model/model.fga}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

log() { echo "authz-bootstrap: $*"; }

store_id="$(fga store list --name "$store_name" \
  | jq -r '.stores // [] | sort_by(.created_at) | .[0].id // empty')"
if [ -z "$store_id" ]; then
  store_id="$(fga store create --name "$store_name" | jq -r '.store.id')"
  log "created store $store_name ($store_id)"
else
  log "found store $store_name ($store_id)"
fi

fga model transform --file "$model_file" --output-format json > "$work/wanted.json"
fga model transform --file "$work/wanted.json" --output-format fga > "$work/wanted.fga"

if fga model get --store-id "$store_id" > "$work/current.fga" 2> "$work/get.err" \
  && cmp -s "$work/current.fga" "$work/wanted.fga"; then
  model_id="$(fga model get --store-id "$store_id" --field id --format json | jq -r '.id')"
  log "model unchanged ($model_id)"
else
  model_id="$(fga model write --store-id "$store_id" --file "$work/wanted.json" \
    | jq -r '.authorization_model_id')"
  log "wrote model $model_id"
fi

log "store_id=$store_id model_id=$model_id"
