# ADR 0054: Aerial photo under the mower map

## Status

Accepted (2026-10-27)

## Context

The mower map draws areas, no-go zones, paths, the mower and the heat layers in the mower's local metres, anchored on the earth by the map's `GeoReference` (origin, rotation; [ADR 0023](0023-map-rendering-and-projection.md)). Without a photo the owner has to imagine the garden behind the outlines; with it, missed spots and obstacles sit on the real lawn.

Forces:

- Photos come from a third party. A browser that loads them reveals the viewer's address and the garden's location to that party and needs a wider Content Security Policy.
- A tile route must not become an open proxy to a third-party service.
- The server-side map snapshot planned for the large map widget needs the same photo without a browser.
- Self-hosters live in other places and use other sources; the code must not send requests anywhere unless the operator asks for it.

Source research (verified 2026-10-27): the Bayerische Vermessungsverwaltung publishes the digital orthophoto at 40 cm as open data, WMS `https://geoservices.bayern.de/od/wms/dop/v1/dop40` (WMS 1.3.0, layers `by_dop40c` colour and `by_dop40g` grey, CRS include EPSG:3857, 4326, 25832; limit 6000 x 6000 px; formats `image/jpeg`, `image/png`). The capabilities document states access constraints "CC BY 4.0" and fees "kostenfrei"; the terms of use (`geodaten.bayern.de/odd/m/3/html/nutzungsbedingungen.html`) require the credit "Bayerische Vermessungsverwaltung - www.geodaten.bayern.de", a link to the licence and a note when changes were made.

## Decision

**Off unless configured.** Telemetry reads `MOWDECK_TELEMETRY_AERIAL_*` (Compose `AERIAL_*`, Helm `config.AERIAL_*`): a WMS base URL with layer (EPSG:3857 `GetMap`, version 1.3.0 or 1.1.1, `image/jpeg` or `image/png`) or an XYZ template, the credit line, zoom range, cache limits and a per-account rate. Empty (the default) means no aerial layer, no toggle in the app and no request to any third party. Sources must be https (a test flag allows http for the fake source of the test stack).

**A tile proxy in Telemetry**, because it already holds the map snapshot and the per-mower authorisation:

- `GET /api/v1/telemetry/mowers/{id}/aerial` tells the client whether a photo is offered, the credit line, zoom range, the box where tiles are served and the tile URL template.
- `GET /api/v1/telemetry/mowers/{id}/aerial/{z}/{x}/{y}` returns one Web Mercator tile. It needs `can_view` on the mower (as every mower route) and serves a tile only when its zoom is in range and its box overlaps the bounding box of everything on that mower's map plus a margin (60 m). Anything else is `404 tile-outside-map`: the route cannot be used to browse the source.
- A missing tile is requested once from the source for exactly the tile's EPSG:3857 box (`BBOX` = the tile, 256 x 256 px), so no client side reprojection exists and the photo lines up with the map's projection (Leaflet in EPSG:3857, local metres converted with the same equirectangular formula as `to_wgs84`). Answers must be PNG or JPEG up to 2 MB (a WMS error document with status 200 is refused); redirects are not followed.
- Tiles are cached on a volume as files named by the hash of the upstream URL: TTL 30 days, size limit 256 MB (oldest deleted first), unwritable directory means no caching. Fetches of tiles not in the cache are limited per account (240 per minute, `429` with `Retry-After`) and run with four in flight at most.
- The route is the single producer of tiles (`Aerial.tile(account, mower, z, x, y)`), so a server side composite of map and photo (widget, notification image) calls it with the same arguments; no composite is built here.

**The browser never contacts the source.** The web app loads the tiles from the route above with its bearer token (`fetch`, blob URLs; an `<img>` cannot send the header), so the CSP is unchanged (`img-src` already has `blob:`, `connect-src 'self'`). The optional browser side raster tile setting (`WEB_MAP_TILES`) stays independent.

**The app.** The mower page gets a toggle "Luftbild" with an opacity slider beside "Maehpfad" in the layer strip, shown only when the installation offers a photo; both are remembered per browser (`mowdeck.aerial`). With the photo on the map uses the geographic mode (north up, as with raster tiles) and shows the credit line.

**Credit.** The line is configured by the operator and always shown while the photo is visible. For Bavaria: "(c) Bayerische Vermessungsverwaltung - www.geodaten.bayern.de (CC BY 4.0)". The resampling into tiles is a change of the data; the ops document tells the operator to keep the line with the licence name.

## Consequences

- One more volume for Telemetry (`aerial-cache`; an emptyDir in the Helm chart).
- With a photo source the source learns the operator's server address and the tile coordinates around the garden, not the users. Documented in the ops guide.
- WMS sources must support EPSG:3857; sources without it are used through an XYZ proxy of the operator's choice.
- The first view of a garden costs some dozens of requests; later views and other users of the map come from the cache.
- Theft tracking reuses the source, the cache and the fetch limits for its base map, but around the tracked positions instead of the garden ([ADR 0058](0058-theft-tracking-mode.md), base map).