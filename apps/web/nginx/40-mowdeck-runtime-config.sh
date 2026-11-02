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
#   /tmp/mowdeck/demo.conf    included by every location that serves the app's HTML: in a demo,
#                             sub_filter rules that put "Demo" into the served page itself (title,
#                             description, Open Graph), so link previews, crawlers and visitors
#                             without JavaScript see the label too; otherwise only a comment.
#                             Only text in <head> changes, no script: the CSP hashes stay valid.
#
# config.json also says whether the image ships /whats-new.json (`whatsNew`, written by the release
# build only), so the app does not request a file that is not there.
#
# MOWDECK_CONFIG_DIR, MOWDECK_CSP_TEMPLATE and MOWDECK_WEB_ROOT replace /tmp/mowdeck, the build CSP
# and the served directory (tests).
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
#   MOWDECK_WEB_DEMO_MODE        true: a public demo installation (ADR 0068): the app shows the
#                                demo banner, "Demo" in the title and "Try the demo" on the
#                                sign-in page (the services enforce the demo themselves)
#   MOWDECK_WEB_DEMO_RESET_HOURS hours between two resets of the demo, shown in the banner
#                                (1 to 168, default 6)
#   MOWDECK_WEB_DEMO_SANDBOX     true (with the demo mode): every visitor gets an own sandbox account
#                                (ADR 0076); the banner and the sign-in page say so, and the app
#                                starts a fresh sandbox when a visitor's one was deleted
#   MOWDECK_WEB_DEMO_SANDBOX_IDLE_MINUTES
#                                minutes after the last visit until a sandbox is deleted, shown
#                                in the banner (5 to 1440, default 30); match Identity's setting
#   MOWDECK_WEB_API_EXPLORER     true: the interactive API explorer at /api-explorer (ADR 0072),
#                                meant for the public demo: "try it" calls run with the visitor's
#                                own session. Anything else: no page, and nginx answers 404 for
#                                every file of its frame (/_explorer/, /tmp/mowdeck/explorer.conf)
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

demo_json=""
case "$(printf '%s' "${MOWDECK_WEB_DEMO_MODE:-false}" | tr '[:upper:]' '[:lower:]')" in
  true | 1 | yes | on)
    reset_hours="${MOWDECK_WEB_DEMO_RESET_HOURS:-6}"
    case "$reset_hours" in
      '' | *[!0-9]*) reset_hours=6 ;;
    esac
    if [ "$reset_hours" -lt 1 ] || [ "$reset_hours" -gt 168 ]; then reset_hours=6; fi
    sandbox_json=""
    idle_minutes=""
    case "$(printf '%s' "${MOWDECK_WEB_DEMO_SANDBOX:-false}" | tr '[:upper:]' '[:lower:]')" in
      true | 1 | yes | on)
        idle_minutes="${MOWDECK_WEB_DEMO_SANDBOX_IDLE_MINUTES:-30}"
        case "$idle_minutes" in
          '' | *[!0-9]*) idle_minutes=30 ;;
        esac
        if [ "$idle_minutes" -lt 5 ] || [ "$idle_minutes" -gt 1440 ]; then idle_minutes=30; fi
        sandbox_json=",\"sandbox\":{\"idleMinutes\":$idle_minutes}"
        ;;
    esac
    demo_json=",\"demo\":{\"resetHours\":$reset_hours$sandbox_json}"
    if [ -n "$sandbox_json" ]; then
      echo "mowdeck: PUBLIC DEMO with a sandbox per visitor (deleted $idle_minutes min after the last visit, reset every $reset_hours h)"
    else
      echo "mowdeck: PUBLIC DEMO (banner on every page, reset every $reset_hours h)"
    fi
    ;;
esac

# The demo label in the served HTML (app.html's <title>MowDeck</title>, once per page).
if [ -n "$demo_json" ]; then
  if [ -n "$sandbox_json" ]; then
    demo_text="Öffentliche MowDeck-Demo: deine eigene Sandbox mit simulierten Mähern, $idle_minutes min nach dem letzten Besuch gelöscht, alle $reset_hours h zurückgesetzt."
  else
    demo_text="Öffentliche MowDeck-Demo mit simulierten Mähern, geteilt mit allen Besuchern, alle $reset_hours h zurückgesetzt."
  fi
  {
    echo "# Public demo (MOWDECK_WEB_DEMO_MODE=true): the label in the page itself."
    echo "sub_filter_once on;"
    echo "sub_filter '<title>MowDeck</title>' '<title>Demo · MowDeck</title><meta name=\"description\" content=\"$demo_text\" /><meta property=\"og:title\" content=\"Demo · MowDeck\" /><meta property=\"og:site_name\" content=\"MowDeck Demo\" /><meta property=\"og:description\" content=\"$demo_text\" />';"
  } > "$out/demo.conf"
else
  echo "# Not a demo: the page is served as built." > "$out/demo.conf"
fi

# The API explorer (ADR 0072): off unless asked for. explorer.conf is included by the /_explorer/
# location of default.conf; off, it answers 404 before any file is looked up.
api_explorer=false
case "$(printf '%s' "${MOWDECK_WEB_API_EXPLORER:-false}" | tr '[:upper:]' '[:lower:]')" in
  true | 1 | yes | on) api_explorer=true ;;
  false | 0 | no | off | '') ;;
  *) echo "mowdeck: MOWDECK_WEB_API_EXPLORER is not true or false; the API explorer stays off" >&2 ;;
esac
if [ "$api_explorer" = true ]; then
  echo "# API explorer on (MOWDECK_WEB_API_EXPLORER=true)" > "$out/explorer.conf"
  echo "mowdeck: API explorer at /api-explorer (try it out with the visitor's own session)"
else
  echo "return 404;" > "$out/explorer.conf"
fi

# The notes of "Was ist neu": only a release build writes them into the image.
whats_new=false
if [ -f "${MOWDECK_WEB_ROOT:-/usr/share/nginx/html}/whats-new.json" ]; then whats_new=true; fi

printf '{"mapTiles":%s,"legalEnabled":%s,"cameraEnabled":%s,"apiExplorer":%s,"whatsNew":%s,"source":%s%s}\n' "$map_tiles" "$legal_enabled" "$camera_enabled" "$api_explorer" "$whats_new" "$source_json" "$demo_json" > "$out/config.json"
sed -e "s#img-src 'self' data: blob:#img-src 'self' data: blob:${img_extra}#" \
  -e "s#connect-src 'self'#connect-src 'self'${connect_extra}#" \
  -e "s#style-src 'self'#style-src 'self'${style_extra}#" \
  -e "s#media-src 'self'#media-src 'self'${media_extra}#" \
  -e "s#worker-src 'self'#worker-src 'self'${worker_extra}#" \
  "$csp_template" > "$out/csp.conf"
