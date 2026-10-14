#!/bin/sh
# Print one NAME_IMAGE=<registry>/<alias>:<tag> line per image of infra/images.txt, for
# example to point the Compose stack or CI at a mirror:
#
#   sh infra/mirror-env.sh ghcr.io/<owner>/mowdeck-mirror >> infra/compose/.env
#
# NAME is the alias in upper case with "-" as "_" (fga-cli -> FGA_CLI_IMAGE).
set -eu
prefix=${1:?usage: mirror-env.sh <registry/prefix>}
list=$(dirname "$0")/images.txt
awk -v prefix="${prefix%/}" '
  /^[[:space:]]*(#|$)/ { next }
  {
    tag = $2; sub(/.*:/, "", tag)
    name = toupper($1); gsub(/-/, "_", name)
    print name "_IMAGE=" prefix "/" $1 ":" tag
  }
' "$list"
