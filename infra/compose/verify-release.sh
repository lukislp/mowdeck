#!/usr/bin/env bash
# Verifies the MowDeck images of a release before the Compose stack runs them, and pins them.
#
#   ./verify-release.sh [--no-pin | --print | --out FILE] <version>
#
# For every image docker-compose.yml takes from ${MOWDECK_IMAGE_REGISTRY} it
#   1. reads the image digest from the notes of the GitHub release v<version> (gh when it is
#      installed and logged in, else the public GitHub API with curl, no token needed),
#   2. checks that the registry tag <version> points to exactly that digest,
#   3. verifies the Sigstore keyless signature of the digest with cosign, bound to the release
#      workflow of the repository (cosign from PATH, else the pinned cosign container),
# and fails closed: nothing is written unless every image passes. Then it writes the Compose
# override docker-compose.release.yml next to this script, which sets every service to
# <registry>/<image>:<version>@sha256:<digest>, so `up` runs exactly the verified images even if
# a tag is moved later (docs/ops/compose.md, "Updating to a release").
#
# Options: --no-pin verifies only; --print writes the override to stdout instead of the file;
# --out FILE writes it to FILE.
#
# Settings (environment first, then the .env next to this script; MOWDECK_ENV_FILE names
# another file):
#   MOWDECK_IMAGE_REGISTRY  registry prefix of the release images (default ghcr.io/lukislp/mowdeck)
#   SOURCE_URL              the repository of the running code (https://github.com/<owner>/<repo>)
#   MOWDECK_RELEASE_REPO    <owner>/<repo> of the releases; default: from SOURCE_URL, else
#                           <registry owner>/mowdeck. A fork sets SOURCE_URL or this.
#   MOWDECK_COSIGN          auto (default), local (cosign from PATH) or container
#   MOWDECK_COSIGN_IMAGE    the cosign container (pinned by digest)
#   MOWDECK_GITHUB_API      GitHub API base URL (default https://api.github.com)
set -euo pipefail

readonly DEFAULT_REGISTRY=ghcr.io/lukislp/mowdeck
readonly DEFAULT_COSIGN_IMAGE=ghcr.io/sigstore/cosign/cosign:v3.0.5@sha256:be924970ba7438c22e18067dec5637946d6566eac711f5bedd1584e7137008fb
readonly ISSUER=https://token.actions.githubusercontent.com

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose_file="$here/docker-compose.yml"

usage() {
  echo "usage: $0 [--no-pin | --print | --out FILE] <version>" >&2
  exit 2
}

die() {
  echo "verify-release: $*" >&2
  exit 1
}

mode=pin
out="$here/docker-compose.release.yml"
version=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-pin) mode=none ;;
    --print) mode=print ;;
    --out)
      [ "$#" -ge 2 ] || usage
      mode=pin
      out="$2"
      shift
      ;;
    -h | --help) usage ;;
    -*) usage ;;
    *)
      [ -z "$version" ] || usage
      version="$1"
      ;;
  esac
  shift
done
version="${version#v}"
[ -n "$version" ] || usage
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || die "not a release version: $version"

# A setting from the environment, else the last KEY= line of the env file (never sourced).
env_file="${MOWDECK_ENV_FILE:-$here/.env}"
setting() {
  local key="$1" value
  if [ -n "${!key:-}" ]; then
    printf '%s' "${!key}"
    return
  fi
  [ -f "$env_file" ] || return 0
  value="$(grep -E "^${key}=" "$env_file" | tail -n 1 | cut -d= -f2- | tr -d '\r')" || true
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

registry="$(setting MOWDECK_IMAGE_REGISTRY)"
registry="${registry:-$DEFAULT_REGISTRY}"
registry="${registry%/}"
case "$registry" in
  */*) ;;
  *) die "MOWDECK_IMAGE_REGISTRY=$registry names local builds; set it to the release registry, e.g. $DEFAULT_REGISTRY" ;;
esac
registry_owner=""
if [[ "$registry" =~ ^ghcr\.io/([^/]+)/ ]]; then
  registry_owner="${BASH_REMATCH[1],,}"
fi

repo="$(setting MOWDECK_RELEASE_REPO)"
if [ -z "$repo" ]; then
  source_url="$(setting SOURCE_URL)"
  if [[ "$source_url" =~ ^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/?$ ]]; then
    repo="${BASH_REMATCH[1]%.git}"
  elif [ -n "$registry_owner" ]; then
    repo="$registry_owner/mowdeck"
  else
    die "cannot tell the release repository; set MOWDECK_RELEASE_REPO=<owner>/<repo>"
  fi
  repo_owner="${repo%%/*}"
  if [ -n "$registry_owner" ] && [ "${repo_owner,,}" != "$registry_owner" ]; then
    die "the images come from $registry but the releases from $repo; set SOURCE_URL or MOWDECK_RELEASE_REPO to the repository that publishes them"
  fi
fi
[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "not a repository: $repo"
identity="https://github.com/$repo/.github/workflows/release.yml@refs/heads/main"

# "service image" for every service whose image is ${MOWDECK_IMAGE_REGISTRY:-...}/<image>:<tag>.
[ -f "$compose_file" ] || die "missing $compose_file"
services="$(awk '
  /^services:/ { in_services = 1; next }
  /^[^ #]/ { in_services = 0 }
  in_services && /^  [A-Za-z0-9_.-]+:[ \t]*$/ { svc = $1; sub(/:$/, "", svc) }
  in_services && /^    image:[ \t]*\$\{MOWDECK_IMAGE_REGISTRY:-[^}]*\}\// {
    ref = $2
    sub(/^\$\{MOWDECK_IMAGE_REGISTRY:-[^}]*\}\//, "", ref)
    sub(/:.*$/, "", ref)
    print svc, ref
  }
' "$compose_file")"
[ -n "$services" ] || die "no service of $compose_file uses MOWDECK_IMAGE_REGISTRY"
images="$(printf '%s\n' "$services" | awk '{print $2}' | sort -u)"

# The release notes list "| `<registry>/<image>` | `sha256:<digest>` |".
tag="v$version"
notes=""
if command -v gh > /dev/null 2>&1; then
  notes="$(gh release view "$tag" -R "$repo" --json body --jq .body 2> /dev/null)" || notes=""
fi
if [ -z "$notes" ]; then
  api="${MOWDECK_GITHUB_API:-https://api.github.com}"
  notes="$(curl -fsSL --proto '=https' --tlsv1.2 -H 'Accept: application/vnd.github+json' \
    "$api/repos/$repo/releases/tags/$tag")" || die "release $tag of $repo not found"
fi
tick='`'
pairs="$(printf '%s' "$notes" | grep -oE "[A-Za-z0-9._/-]+$tick \| ${tick}sha256:[0-9a-f]{64}" |
  sed -E "s/$tick \| $tick/ /")" || true
[ -n "$pairs" ] || die "release $tag of $repo lists no image digests"

if [ "${MOWDECK_COSIGN:-auto}" = auto ]; then
  if command -v cosign > /dev/null 2>&1; then MOWDECK_COSIGN=local; else MOWDECK_COSIGN=container; fi
fi
case "$MOWDECK_COSIGN" in
  local | container) ;;
  *) die "MOWDECK_COSIGN must be auto, local or container" ;;
esac
cosign_verify() {
  case "$MOWDECK_COSIGN" in
    local) cosign verify "$1" --certificate-identity "$identity" --certificate-oidc-issuer "$ISSUER" ;;
    container)
      docker run --rm "${MOWDECK_COSIGN_IMAGE:-$DEFAULT_COSIGN_IMAGE}" verify "$1" \
        --certificate-identity "$identity" --certificate-oidc-issuer "$ISSUER"
      ;;
  esac
}

echo "MowDeck $version: $registry, signed by $identity" >&2
fail=0
declare -A digest_of=()
for image in $images; do
  ref="$registry/$image"
  digests="$(printf '%s\n' "$pairs" | awk -v r="$ref" '$1 == r {print $2}' | sort -u)"
  if [ -z "$digests" ]; then
    echo "  $image: NOT IN RELEASE $tag" >&2
    fail=1
    continue
  fi
  if [ "$(printf '%s\n' "$digests" | wc -l)" -ne 1 ]; then
    echo "  $image: the release lists more than one digest" >&2
    fail=1
    continue
  fi
  digest="$digests"
  tag_digest="$(docker buildx imagetools inspect "$ref:$version" --format '{{json .Manifest.Digest}}' 2> /dev/null | tr -d '"\r')" || tag_digest=""
  if [ "$tag_digest" != "$digest" ]; then
    echo "  $image: TAG MISMATCH $ref:$version is ${tag_digest:-missing}, the release says $digest" >&2
    fail=1
    continue
  fi
  if ! log="$(cosign_verify "$ref@$digest" 2>&1 > /dev/null)"; then
    echo "  $image: SIGNATURE INVALID for $ref@$digest" >&2
    printf '%s\n' "$log" | tail -n 3 | sed 's/^/    /' >&2
    fail=1
    continue
  fi
  echo "  $image: ok $digest" >&2
  digest_of["$image"]="$digest"
done
[ "$fail" -eq 0 ] || die "release $version NOT verified; nothing pinned"

override() {
  echo "# Generated by verify-release.sh: MowDeck $version, verified $(date -u +%Y-%m-%dT%H:%M:%SZ)."
  echo "# Every image below is the digest of release $tag of $repo, signed by"
  echo "# $identity."
  echo "# Use: docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --no-build --wait"
  echo "services:"
  printf '%s\n' "$services" | while read -r svc image; do
    echo "  $svc:"
    echo "    image: $registry/$image:$version@${digest_of[$image]}"
  done
}

case "$mode" in
  none) echo "verified; not pinned (--no-pin)" >&2 ;;
  print) override ;;
  pin)
    tmp="$(mktemp "$out.XXXXXX")"
    trap 'rm -f "$tmp"' EXIT
    override > "$tmp"
    chmod 644 "$tmp"
    mv "$tmp" "$out"
    trap - EXIT
    echo "pinned: $out" >&2
    ;;
esac
