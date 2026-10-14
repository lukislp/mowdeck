# ADR 0023: Map rendering and projection

## Status

Accepted (2026-10-09)

## Context

The map contract keeps all geometry in the mower's local frame: `MowerMap` holds areas, no-go zones, obstacles, paths and the dock as `(x, y)` in metres plus a `GeoReference` (origin latitude and longitude, rotation of the local axes), because that is what RTK mowers such as the Mammotion LUBA store and what planning (patterns, route checks, later spot mowing) works in. Live positions in `MowerState` and the Telemetry position track are WGS84. The web app (Task 11) must show the areas, the dock, the mower with its heading and the driven route, let the user choose start areas on the map, and look like part of the "instrument panel" ([Design: web app](../dev/design-web-app.md), section 12).

Forces:

- A self-hosted, privacy-minded product: a map background from a public tile server sends the tile coordinates, that is the location of the garden, and the viewer's IP address to a third party on every view, and the public OpenStreetMap tile servers have a usage policy that does not fit an app (https://operations.osmfoundation.org/policies/tiles/).
- The geometry is a few hundred metres at most; a metric frame is what the user thinks in ("the front lawn is 12 by 8 m").
- Leaflet 1.9.4 is stable; 2.0 is still alpha. Editing (Geoman) is not needed until no-go zones or spot mowing are edited in the app.
- The strict CSP (no inline styles, no external hosts) must hold.

## Decision

**Leaflet 1.9.4, loaded lazily, wrapped by `MowerMap` in `libs/mowdeck-ui`.** The component creates the Leaflet map in an effect bound to its container and tears it down in the cleanup; no wrapper library. Leaflet is a dynamic import, so its 150 kB stay out of the initial bundle; when it cannot load, the page falls back to the schematic `AreaPlan`.

**Two projection modes behind one adapter** (`libs/mowdeck-ui/src/lib/map/projection.ts`):

- **`local` (default): `L.CRS.Simple` in local metres.** A Leaflet `[lat, lng]` is `[y, x]`; the background is a metric grid (1 m and 5 m lines) drawn as a canvas tile layer that covers any pan and zoom and thins out when zoomed far out, the scale bar reads metres and the north arrow is rotated by the reference rotation. No tile request leaves the installation.
- **`geo`: `EPSG:3857` with raster tiles**, only when the installation configures a tile URL template (`MOWDECK_WEB_MAP_TILES`, rendered into `/config.json` and the CSP `img-src` at container start). Local metres are converted to WGS84 for display.

Both modes share the overlay code: the adapter converts local points, WGS84 positions and headings into map coordinates and screen rotations.

**Conversion happens in the browser** from the map's `GeoReference`, with an exact TypeScript port of `mowdeck_control.geo` (`libs/mowdeck-ui/src/lib/map/geo.ts`: `toWgs84`, `fromWgs84`, `compassHeading`). The wire format stays local metres; there is no second (GeoJSON) representation to keep in sync. `libs/mowdeck-ui/scripts/geo_vectors.py` prints golden vectors from the Python implementation (four rotations, four origins, eight points and five angles each) into a committed JSON fixture, and a Vitest test requires agreement within 1e-9 degrees.

**A pure layer model** (`layers.ts`) turns snapshot, state and route into layer descriptors (areas with labels and selection order, zones, paths, dock, mower with tone and rotation, route segments by age and optional RTK class, grid, bounds); it is unit tested without a DOM, and the component only draws descriptors. Styles are CSS classes on Leaflet's SVG paths and on our own marker elements, using the design tokens, so light and dark switch without redrawing. Leaflet's default controls, markers and attribution box are not used: zoom, fit, follow, layers and full screen are 48 px token-styled buttons rendered by the component; the attribution (tile mode only) is a plain text line.

**Geoman is deferred** to the first editing feature (no-go zone edits or spot mowing).

Options considered: converting to GeoJSON on the server (a second representation, and the metre frame that later editing needs would be lost in the client); MapLibre GL (vector tiles and WebGL, far more code than a garden needs, and still a tile source to configure); keeping the schematic SVG plan (no pan, zoom, gestures or live marker; it stays as the fallback); always using `EPSG:3857` with an empty background (no benefit without tiles, and the grid would no longer line up with the mower's frame).

## Consequences

- Without configuration the map works offline-first, needs no third party and no CSP exception; satellite or street context is an explicit, documented opt-in per installation with its privacy cost stated.
- The TypeScript port is a second implementation of the conversion; the golden vectors catch drift, and the script must be re-run after a change to `geo.py`.
- In `local` mode the map is not north-up but frame-up (the north arrow shows the difference); headings are rotated accordingly by the adapter.
- Leaflet DOM rendering is covered by the Playwright gate (areas, dock, a moving chevron, area selection), not by unit tests.
- A tile provider other than OpenStreetMap may need an API key in the URL template; it is visible to every viewer of the app (no secrets in `/config.json`).
