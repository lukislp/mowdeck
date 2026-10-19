#!/bin/sh
# The third-party images pinned under "Image sources" in infra/compose/.env.example (one
# NAME_IMAGE=<image:tag> line per image) as references into a mirror registry.
#
#   sh infra/mirror-env.sh [<registry/prefix>]
#       prints NAME_IMAGE=<prefix>/<alias>:<tag>[@<digest>] per image (the digest when the list
#       pins one, so a pull from the mirror is checked against it). The alias is the variable name without
#       _IMAGE in lower case with "-" for "_" (POSTGRES_IMAGE -> postgres, FGA_CLI_IMAGE -> fga-cli,
#       STRIPE_MOCK_IMAGE -> stripe-mock); the prefix is the argument or $MOWDECK_IMAGE_MIRROR,
#       lower-cased as registries require. CI appends the lines to $GITHUB_ENV and to .env; at home:
#       MOWDECK_IMAGE_MIRROR=registry.example.org/mowdeck sh infra/mirror-env.sh >> infra/compose/.env
#   sh infra/mirror-env.sh --list
#       prints "<alias> <source image:tag>" per image: what .github/workflows/mirror-images.yml copies.
set -eu
list=$(dirname "$0")/compose/.env.example
mode=env
prefix=
case "${1:-}" in
  --list) mode=list ;;
  *) prefix=${1:-${MOWDECK_IMAGE_MIRROR:?usage: mirror-env.sh <registry/prefix> | --list}} ;;
esac
prefix=$(printf '%s' "${prefix%/}" | tr '[:upper:]' '[:lower:]')

awk -v mode="$mode" -v prefix="$prefix" '
  /^[A-Z][A-Z0-9_]*_IMAGE=/ {
    name = $0; sub(/=.*/, "", name)
    source = $0; sub(/^[^=]*=/, "", source); sub(/[[:space:]]+$/, "", source)
    alias = tolower(substr(name, 1, length(name) - 6)); gsub(/_/, "-", alias)
    ref = source; digest = ""
    at = index(ref, "@")
    if (at > 0) { digest = substr(ref, at); ref = substr(ref, 1, at - 1) }
    tag = ref; sub(/.*:/, "", tag)
    if (mode == "list") print alias, source
    else print name "=" prefix "/" alias ":" tag digest
  }
' "$list"
