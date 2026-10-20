#!/bin/sh
# Renders the runtime configuration of the web app at container start (a hook of the nginx
# image's entrypoint in /docker-entrypoint.d). The container runs read-only, so both files go to
# the tmpfs at /tmp:
#
#   /tmp/mowdeck/config.json  served at /config.json; the app reads it once at boot
#   /tmp/mowdeck/csp.conf     the Content Security Policy from the build (build/csp.conf), with
#                             the tile server added to img-src when tiles are configured and
#                             the Agora domains and the hash of the SDK's one inline style
#                             added when the live camera is enabled
#
# MOWDECK_CONFIG_DIR and MOWDECK_CSP_TEMPLATE replace /tmp/mowdeck and the build CSP (tests).
#
# Environment (all optional; empty keeps the metric grid and sends nothing to third parties):
#   MOWDECK_WEB_MAP_TILES        raster tile URL template, e.g. https://tile.example.org/{z}/{x}/{y}.png
#   MOWDECK_WEB_MAP_ATTRIBUTION  attribution text the tile provider requires
#   MOWDECK_WEB_MAP_MAX_ZOOM     highest zoom the tile server delivers (1 to 22, default 19)
#   MOWDECK_WEB_LEGAL_ENABLED    true shows links to /legal/imprint, /legal/terms and
#                                /legal/privacy on the sign-in page (default false)
#   MOWDECK_WEB_CAMERA_ENABLED   true: allow the live camera of cloud mowers (ADR 0032). The browser
#                                then connects to Agora (*.agora.io, *.sd-rtn.com, any port) and plays the
#                                video from blob: URLs; the video goes through Agora's and the
#                                vendor's servers. Anything else keeps them out of the CSP.
#
# The build and its source code (AGPL-3.0 section 13, LICENSING.md), baked into the image by the
# release build and overridable here; the app shows them on the sign-in page and under Settings:
#   MOWDECK_VERSION              version of the build (empty: a development build)
#   MOWDECK_SOURCE_URL           repository of the running code (default the official one); a fork
#                                that runs modified code sets its own; {ref} makes it a template
#   MOWDECK_SOURCE_REF           git ref of the build (the tag v<version> or a commit)
set -eu

out="${MOWDECK_CONFIG_DIR:-/tmp/mowdeck}"
csp_template="${MOWDECK_CSP_TEMPLATE:-/etc/nginx/snippets/csp.conf}"
mkdir -p "$out"

tiles="${MOWDECK_WEB_MAP_TILES:-}"
attribution="$(printf '%s' "${MOWDECK_WEB_MAP_ATTRIBUTION:-}" | tr -d '\000-\037')"
max_zoom="${MOWDECK_WEB_MAP_MAX_ZOOM:-19}"

json_string() {
  printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
}

img_extra=""
map_tiles="null"
if [ -n "$tiles" ]; then
  # Scheme and host of the template; a {s} subdomain placeholder becomes a CSP wildcard.
  origin="$(printf '%s' "$tiles" | sed -n -E 's#^(https?://[^/]+)/.*$#\1#p' | sed 's/{s}/*/')"
  if printf '%s' "$tiles" | grep -Eq '^https?://[^[:space:]"<>;\\]+$' \
    && printf '%s' "$tiles" | grep -q '{z}' \
    && printf '%s' "$tiles" | grep -q '{x}' \
    && printf '%s' "$tiles" | grep -q '{y}' \
    && printf '%s' "$origin" | grep -Eq '^https?://(\*\.)?[A-Za-z0-9.-]+(:[0-9]+)?$'; then
    case "$max_zoom" in
      '' | *[!0-9]*) max_zoom=19 ;;
    esac
    if [ "$max_zoom" -lt 1 ] || [ "$max_zoom" -gt 22 ]; then max_zoom=19; fi
    if [ -n "$attribution" ]; then attr_json="$(json_string "$attribution")"; else attr_json="null"; fi
    map_tiles="{\"url\":$(json_string "$tiles"),\"attribution\":$attr_json,\"maxZoom\":$max_zoom}"
    img_extra=" $origin"
    echo "mowdeck: map tiles from $origin (the tile server learns the location of the map)"
  else
    echo "mowdeck: MOWDECK_WEB_MAP_TILES is not a valid http(s) tile URL template with {z}, {x} and {y}; showing the grid only" >&2
  fi
fi

legal_enabled=false
case "$(printf '%s' "${MOWDECK_WEB_LEGAL_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')" in
  true | 1 | yes | on) legal_enabled=true ;;
  false | 0 | no | off | '') ;;
  *) echo "mowdeck: MOWDECK_WEB_LEGAL_ENABLED is not true or false; legal links stay hidden" >&2 ;;
esac
if [ "$legal_enabled" = true ]; then
  echo "mowdeck: links to the legal pages (/legal/*.md) on the sign-in page"
fi

# Only an http(s) URL without whitespace, quotes, angle brackets or backslashes, and plain versions
# and refs; anything else is left out and the app links the official repository.
source_url="${MOWDECK_SOURCE_URL:-}"
if ! printf '%s' "$source_url" | grep -Eq '^https?://[^[:space:]"<>\\]+$'; then
  if [ -n "$source_url" ]; then
    echo "mowdeck: MOWDECK_SOURCE_URL is not an http(s) URL; linking the official repository" >&2
  fi
  source_url=""
fi
source_version="${MOWDECK_VERSION:-}"
printf '%s' "$source_version" | grep -Eq '^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$' || source_version=""
source_ref="${MOWDECK_SOURCE_REF:-}"
printf '%s' "$source_ref" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$' || source_ref=""
source_json="{\"url\":$(json_string "$source_url"),\"version\":$(json_string "$source_version"),\"ref\":$(json_string "$source_ref")}"
echo "mowdeck: build ${source_version:-dev}, source code at ${source_url:-the official repository}${source_ref:+ ($source_ref)}"

camera_enabled=false
connect_extra=""
media_extra=""
worker_extra=""
style_extra=""
case "${MOWDECK_WEB_CAMERA_ENABLED:-false}" in
  true | TRUE | True | 1 | yes | on)
    camera_enabled=true
    # Agora's gateways, edge servers, proxies and event reporting (agora-rtc-sdk-ng 4.24). The
    # SDK decodes in a blob: worker and the page plays the video from the remote track. A CSP
    # host source without a port matches the scheme's default port only, and the edge servers
    # listen on others (wss://208-81-200-218.edge.agora.io:4718/): each host also with ':*'.
    agora_hosts="https://*.agora.io wss://*.agora.io https://*.sd-rtn.com wss://*.sd-rtn.com"
    agora_ports="https://*.agora.io:* wss://*.agora.io:* https://*.sd-rtn.com:* wss://*.sd-rtn.com:*"
    connect_extra=" $agora_hosts $agora_ports"
    media_extra=" blob:"
    worker_extra=" blob:"
    # agora-rtc-sdk-ng 4.24.8 sets one inline style itself, on every remote video track: a 1 px
    # probe <video> that measures the resolution (setAttribute("style", "position: fixed; top: 0;
    # left: 0; width: 1px; height: 1px"), the function that starts with
    # document.createElement("video") in the SDK chunk). The build's style-src already has
    # 'unsafe-hashes' for the app's own style attributes; this is the hash of exactly that
    # string. Re-check it when the SDK version changes (the browser console names a new hash).
    style_extra=" 'sha256-cPxxYHvCiQ/kT6TlojjBofFPdVQmCg8k/CdPKQDf9zI='"
    echo "mowdeck: live camera enabled (video through Agora; the CSP allows *.agora.io and *.sd-rtn.com on any port)"
    ;;
esac

printf '{"mapTiles":%s,"legalEnabled":%s,"cameraEnabled":%s,"source":%s}\n' "$map_tiles" "$legal_enabled" "$camera_enabled" "$source_json" > "$out/config.json"
sed -e "s#img-src 'self' data: blob:#img-src 'self' data: blob:${img_extra}#" \
  -e "s#connect-src 'self'#connect-src 'self'${connect_extra}#" \
  -e "s#style-src 'self'#style-src 'self'${style_extra}#" \
  -e "s#media-src 'self'#media-src 'self'${media_extra}#" \
  -e "s#worker-src 'self'#worker-src 'self'${worker_extra}#" \
  "$csp_template" > "$out/csp.conf"
