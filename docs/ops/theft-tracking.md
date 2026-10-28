# Theft tracking mode

When a theft alarm fires (lifted, left the dock without a command, left the garden) MowDeck starts **tracking** the mower: a position every 10 seconds over whatever link the mower has (the cloud over 4G for a Mammotion), for at most 24 hours. Design and decisions: [ADR 0058](../adr/0058-theft-tracking-mode.md).

## What the owner sees

- A notification "Ortung gestartet" (critical, also during quiet hours). Tapping it opens `/mowers/<id>/tracking`.
- On that page: a big map with the track since the alarm on a street map or aerial photo ([Base map](#base-map)), the last position and its age, the coordinates as text with a copy button and links to Apple Maps, Google Maps and OpenStreetMap, **Link teilen**, **Bericht (PDF)** and **Ortung beenden**. Ending asks for the reason: **Fehlalarm** or **Ortung beenden**. The mower page has a shield button that turns red while tracking runs.
- Further notifications are rare on purpose: a move of 50 m or more (at most every 5 minutes), "Kontakt verloren" once per outage (the last position stays on the map, "stale since"), and the end (by hand or after the maximum time).

Starting needs `can_control` on the mower, viewing the track, the links and the report needs `can_view`.

## Try it safely

Start tracking by hand on the tracking page ("Ortung starten"): it takes the same path as an alarm (lease to the gateway, notifications, track, share link, report) without lifting the mower or leaving the garden. End it with "Fehlalarm". A geofence test works too: drive the mower out of its mapped area by hand with the margin in mind ([ADR 0027](../adr/0027-health-history-and-theft-alarm.md)), which triggers the real alarm and the automatic start.

## Share link

"Link teilen" creates a secret address `https://<host>/track#<token>`. Whoever has it sees, without a login, a live map with the position, the track since the alarm, the times, and the mower's name and model; nothing else (no account, no other mowers, no serial number, no controls). It expires after 48 hours at the latest and can be revoked in the tracking view at any time; the list shows created, expiry and number of views. The token is 256 bits, only its SHA-256 is stored, and it sits in the URL fragment so it never appears in logs or in the `Referer`. The page and API answer with `noindex` and `no-referrer` headers and are rate limited per client. Treat the link like a key: send it to the police or a trusted person, not to a public chat.

## Base map

A stolen mower can be carried hundreds of kilometres away, so the tracking map needs a real map of wherever it is, not the garden map. Telemetry serves up to two background layers, switchable on the map ("Karte" / "Luftbild"), for the owner's view and for the share link. The map always fits the whole track, wherever it lies.

| Layer | Setting | Coverage |
| --- | --- | --- |
| **Karte** (street map), recommended source **OpenStreetMap** | `BASEMAP_URL=https://tile.openstreetmap.org/{z}/{x}/{y}.png`, `BASEMAP_ATTRIBUTION=© OpenStreetMap contributors`, `BASEMAP_MAX_ZOOM=19` | worldwide, street level up to zoom 19 |
| **Karte**, alternative **BKG TopPlusOpen** (Bundesamt für Kartographie und Geodäsie, open data dl-de/by-2-0, no key) | `BASEMAP_URL=https://sgx.geodatenzentrum.de/wmts_topplus_open/tile/1.0.0/web/default/WEBMERCATOR/{z}/{y}/{x}.png`, `BASEMAP_MAX_ZOOM=18`, credit `© BKG (2026), dl-de/by-2-0, Datenquellen: sgx.geodatenzentrum.de/web_public/gdz/datenquellen/Datenquellen_TopPlusOpen.html` | world up to zoom 12; Europe up to about zoom 16 to 17 (checked: Madrid, Warsaw, Istanbul, Moscow); Germany up to zoom 18. Outside Europe deeper zooms are empty tiles (checked: New York, Tokyo from zoom 13) |
| **Luftbild** (aerial photo) | the aerial photo source of the mower map ([aerial-photo.md](aerial-photo.md), `AERIAL_*`) | that source's area only; the Bavarian orthophoto covers Bavaria only: set `AERIAL_COVERAGE=47.27,8.97,50.57,13.84` |

The street map is **off by default** (no request to any third party until the operator sets it). The credit line is always shown while a layer is visible; an OpenStreetMap credit links to https://www.openstreetmap.org/copyright.

**Coverage and fallback.** `BASEMAP_COVERAGE` and `AERIAL_COVERAGE` (`south,west,north,east` in degrees, empty: the world) tell Telemetry where a source has data. The tracking status says per layer whether it covers the last position (`covers_last`); the map then shows the chosen layer only where it has data and otherwise switches to the next one (for example from Luftbild to Karte once the mower leaves Bavaria) with a hint, or to a grid with a hint when no layer covers the place. A layer whose tiles all fail to load is skipped the same way. Tiles outside a coverage box are refused before any upstream request.

**Coordinates as text.** Under the map both pages show the last position as decimal latitude and longitude with six places (about 10 cm), the time of the fix and its age, a copy button, and links "In Apple Karten öffnen", "In Google Maps öffnen" and "In OpenStreetMap öffnen" (plus the Android maps app via `geo:`). They are built from the coordinates only, so a share token never appears in them. This works even where no map covers the place; give these coordinates to the police. The vendor reports no accuracy figure; without RTK the position can be several metres off.

**How tiles are fetched.** The browser asks MowDeck for each tile (`GET /api/v1/telemetry/mowers/{id}/tracking/map/{layer}/{z}/{x}/{y}` for the owner, `POST /api/v1/telemetry/public/tracking/tile` with the token in the body for the share link). Telemetry serves only tiles that overlap the session's positions plus `MOWDECK_TELEMETRY_TRACKING_MAP_MARGIN_METERS` (2000) and lie in the layer's zoom range and coverage, and keeps them in the aerial cache volume (256 MB). So the browser never contacts the provider, the content security policy stays as it is, and a share link reveals nothing beyond the track it already shows. The provider sees the server's address and the tiles around the track.

**OpenStreetMap tile usage policy** (https://operations.osmfoundation.org/policies/tiles/). Telemetry, which is the only client of the tile server:

- identifies itself on every upstream request: `User-Agent: MowDeck/<version> (+https://github.com/lukislp/mowdeck)`, and sends no `Referer` (none is forged either);
- keeps every tile as long as the server's `Cache-Control: max-age` or `Expires` allows and does not ask again before that; without either header it keeps a tile for `AERIAL_CACHE_TTL_DAYS` (30), never less than 7 days; `no-store`/`no-cache` answers are not kept;
- fetches only tiles a viewer is looking at: no prefetching, no bulk download, no tiles outside the track's box;
- limits new tiles per account (`MOWDECK_TELEMETRY_AERIAL_MISSES_PER_MINUTE`, 240) with four requests in flight at most, and the share page per client (600 tiles per minute);
- shows the attribution with a link to the copyright page.

Keep it that way for a family-sized installation; an operator with heavy use needs its own tile server or a commercial provider.

## Theft report (PDF)

Mower model, serial number or device name (from the connection options, Mammotion `device_name`), the MowDeck name, alarm kind and time, the track (position table and a drawing on a blank metre grid; no map tiles are used) and up to four photos of the mower camera (one is taken at the start and one a minute later if the first failed; more by `POST .../tracking/photo`). Blank fields for name, address, contact, police report number and insurance. German or English (`?lang=`).

## Settings (Telemetry)

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOWDECK_TELEMETRY_TRACKING_INTERVAL_SECONDS` | `10` | Position report interval (the adapter never goes below 5) |
| `MOWDECK_TELEMETRY_TRACKING_MAX_HOURS` | `24` | A session ends by itself after this long |
| `MOWDECK_TELEMETRY_TRACKING_AUTO_START` | `true` | Start on a theft alarm |
| `MOWDECK_TELEMETRY_TRACKING_STALE_AFTER_SECONDS` | `120` | No report for this long marks the session stale |
| `MOWDECK_TELEMETRY_TRACKING_MOVE_METERS` / `_MOVE_NOTICE_SECONDS` | `50` / `300` | Distance and minimum spacing of "moved" notices |
| `MOWDECK_TELEMETRY_TRACKING_LINK_TTL_HOURS` / `_MAX_LINKS` | `48` / `10` | Share link lifetime and active links per session |
| `MOWDECK_TELEMETRY_TRACKING_RETENTION_DAYS` | `30` | Ended sessions, links and photos are deleted this long after the end |
| `MOWDECK_TELEMETRY_TRACKING_PHOTOS` | `true` | Take camera photos for the report |
| `MOWDECK_TELEMETRY_TRACKING_PUBLIC_RATE_PER_MINUTE` / `_PUBLIC_FAILURES_PER_MINUTE` | `60` / `20` | Rate limits of the public page per client |
| `MOWDECK_TELEMETRY_TRACKING_TICK_SECONDS` / `_LEASE_SECONDS` | `15` / `60` | Background tick and gateway lease |
| `MOWDECK_TELEMETRY_BASEMAP_URL` / `_ATTRIBUTION` | empty | Street map under the track (see [Base map](#base-map)); empty: off |
| `MOWDECK_TELEMETRY_BASEMAP_MIN_ZOOM` / `_MAX_ZOOM` | `8` / `19` | Zoom range of the street map |
| `MOWDECK_TELEMETRY_BASEMAP_COVERAGE` / `MOWDECK_TELEMETRY_AERIAL_COVERAGE` | empty (the world) | Box a source covers, `south,west,north,east`; outside it the map switches layers |
| `MOWDECK_TELEMETRY_TRACKING_MAP_MARGIN_METERS` | `2000` | Base map tiles are served this far around the session's positions |
| `MOWDECK_TELEMETRY_TRACKING_PUBLIC_TILES_PER_MINUTE` | `600` | Base map tiles per minute and client on the share page |

## Operating notes

- The vendor cloud sees one report request per interval per tracked mower; the adapter backs off (up to 120 s) when the mower stays silent. If Telemetry stops, the gateway returns to its normal rhythm within a minute (the lease runs out).
- The public page needs the web app's nginx (`/track` is served with `no-store`, `noindex` and `no-referrer`) and Traefik routing of `/api/v1/telemetry` as for every Telemetry route.
- Metrics: `mowdeck_device_gateway_tracking_leases_total{outcome}`.
- Privacy: ended sessions are deleted after 30 days, or at once with `DELETE /api/v1/telemetry/mowers/{id}/tracking/sessions/{session}`; the positions stay in the mower's track under its own retention.