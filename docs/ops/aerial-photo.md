# Aerial photo under the map

MowDeck can lay an aerial photo under the mower map, so areas, no-go zones, the mower's track and the heat layers (coverage, RTK, WLAN, mobile) are seen on the real garden. It is **off by default**: nothing is requested from any third party until you configure a source. Design and reasons: [ADR 0054](../adr/0054-aerial-photo-under-the-map.md).

## How it works

Telemetry fetches the photo tiles itself and serves them to signed-in users at `GET /api/v1/telemetry/mowers/{id}/aerial/{z}/{x}/{y}`:

- only for users who may see that mower, and only for tiles around that mower's map (its outline plus 60 m); anything else is a 404, so the route is no open proxy,
- each tile is requested once from the source and cached on the `aerial-cache` volume (30 days, 256 MB by default, oldest deleted first),
- new tiles are limited to 240 per minute and account,
- browsers never contact the source, so the web app's content security policy does not change.

The source learns your server's address and the tile coordinates around your garden, not who uses MowDeck.

## Bavaria (Bayerische Vermessungsverwaltung, digital orthophoto 40 cm)

Open data under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.de), no registration, [terms of use](https://www.geodaten.bayern.de/odd/m/3/html/nutzungsbedingungen.html). In `infra/compose/.env`:

```
AERIAL_WMS_URL=https://geoservices.bayern.de/od/wms/dop/v1/dop40
AERIAL_WMS_LAYER=by_dop40c
AERIAL_IMAGE_FORMAT=image/jpeg
AERIAL_ATTRIBUTION=© Bayerische Vermessungsverwaltung – www.geodaten.bayern.de (CC BY 4.0)
AERIAL_MAX_ZOOM=19
```

then `docker compose up -d telemetry`. `by_dop40g` is the grey-scale layer. The photo is a tile mosaic of the service's image, scaled; the credit line below the map is the attribution the licence asks for and must stay. Other German states publish similar open orthophoto WMS; any WMS that answers `GetMap` in EPSG:3857 works, or set `AERIAL_XYZ_URL` to a Web Mercator tile template (`https://…/{z}/{x}/{y}.jpg`) that your licence allows you to use this way.

## Settings

All are variables of Telemetry (`MOWDECK_TELEMETRY_AERIAL_*`); Compose passes the first seven from `.env` (`AERIAL_*`), Helm from `config.AERIAL_*`.

| Compose / Helm | Telemetry variable | Default | Meaning |
| --- | --- | --- | --- |
| `AERIAL_WMS_URL` | `…_WMS_URL` | empty (off) | WMS base URL (https) |
| `AERIAL_WMS_LAYER` | `…_WMS_LAYER` | empty | layer name |
| `AERIAL_IMAGE_FORMAT` | `…_IMAGE_FORMAT` | `image/jpeg` | `image/jpeg` or `image/png` |
| `AERIAL_XYZ_URL` | `…_XYZ_URL` | empty | XYZ template instead of the WMS |
| `AERIAL_ATTRIBUTION` | `…_ATTRIBUTION` | empty | credit line shown on the map |
| `AERIAL_MAX_ZOOM` | `…_MAX_ZOOM` | `19` | deepest level with detail; deeper zooms scale it |
| `AERIAL_CACHE_MAX_MB` | `…_CACHE_MAX_MB` | `256` | cache size limit |
| | `…_WMS_VERSION` | `1.3.0` | `1.3.0` or `1.1.1` |
| | `…_MIN_ZOOM` | `14` | lowest level served |
| | `…_TILE_SIZE` | `256` | pixels per WMS request |
| | `…_MARGIN_METERS` | `60` | tiles are served this far around the map |
| | `…_CACHE_TTL_DAYS` | `30` | a cached tile is fetched again after this |
| | `…_MISSES_PER_MINUTE` | `240` | new tiles per account and minute |
| | `…_TIMEOUT_SECONDS` | `10` | timeout per source request |
| | `…_CACHE_DIR` | `/var/cache/mowdeck/aerial` | cache directory (a volume) |

Telemetry logs `aerial photo layer on` at startup when a source is set.

## Checks

- `GET /api/v1/telemetry/mowers/{id}/aerial` (with a token) answers `enabled: true` with the credit line and the box where tiles are served.
- No toggle "Luftbild" in the app means the source is not configured (or the mower has no map yet).
- A blank grey area on the map at high zoom is the source having no image there (outside its coverage).