# @mowdeck/ui

The MowDeck design system: design tokens and the Svelte 5 components of the "instrument panel" (sober, dense, monospaced readouts, colour means state). Used by `apps/web`; usable by any other Svelte 5 frontend (an operator console, a Capacitor shell).

Licence: Apache-2.0 (see `LICENSE`). It is a **pnpm** package (`@mowdeck/ui`) of the frontend workspace, not a uv package. Decision: [ADR 0020](../../docs/adr/0020-web-app-architecture.md); visual direction: [Design: web app](../../docs/dev/design-web-app.md), section 12; developer guide: [docs/dev/web-app.md](../../docs/dev/web-app.md).

## Rules

- No fetch, no global state, no i18n runtime: every text arrives as a prop (or a snippet), every permission decision as a boolean.
- Styles are scoped Svelte styles that use only the tokens (`var(--md-...)`); no utility framework, no component kit.
- The package exports its Svelte and TypeScript sources directly (no build step); the consumer's Vite compiles them.

```svelte
<script lang="ts">
  import '@mowdeck/ui/styles/fonts.css';
  import '@mowdeck/ui/styles/tokens.css';
  import '@mowdeck/ui/styles/base.css';
  import { Readout, StatusBadge } from '@mowdeck/ui';
</script>
```

## Tokens

`src/lib/styles/tokens.css` defines every token on `:root` for the light theme and overrides them for dark in `@media (prefers-color-scheme: dark) { :root:not([data-theme='light']) { ... } }` and again in `:root[data-theme='dark']`, so the system theme applies unless the app sets `data-theme`.

| Group                    | Tokens                                                                                                                                                  |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Neutrals ("Graphite")    | `--md-color-bg`, `-surface`, `-surface-sunken`, `-line`, `-line-strong`, `-ink`, `-ink-muted`, `-ink-inverse`, `-focus` (the only blue)                 |
| States ("Field signals") | `--md-state-mowing`, `-returning`, `-charging`, `-docked`, `-paused`, `-error`, `-offline`, `-unknown`, each with a `-bg` tint; RTK quality reuses them |
| Stop ("Signal stop")     | `--md-stop-fill`, `--md-stop-ink`, `--md-stop-ring`: reserved for `EmergencyStop`                                                                       |
| Type                     | `--md-font-sans` (IBM Plex Sans variable), `--md-font-mono` (IBM Plex Mono), size scale `--md-font-size-11` to `-32`                                    |
| Spacing                  | 4 px grid: `--md-space-0_5` (2 px) to `--md-space-12` (48 px)                                                                                           |
| Radius                   | `--md-radius-0` to `--md-radius-3` (0 to 8 px), `--md-radius-round`                                                                                     |
| Motion                   | `--md-motion-fast` (120 ms), `--md-motion-base` (200 ms), `--md-motion-flash` (300 ms)                                                                  |
| Targets                  | `--md-target` (48 px), `--md-target-command` (56 px), `--md-target-stop` (64 px)                                                                        |

`base.css` resets and sets the typography, `fonts.css` loads the self-hosted Plex fonts (from `@fontsource-variable/ibm-plex-sans` and `@fontsource/ibm-plex-mono`, no external host). `tokens.ts` exports the token names as typed constants for code that needs values (read them with `getComputedStyle`).

The contrast check adjusted a few values of the design: light paused `#8A5200`, light offline and unknown `#5F655F`, dark `--md-color-line-strong` `#6A7279`, dark stop fill `#D0201A` (as in light).

## Components

| Component                      | Purpose                                                                                                                                                                                                                                                                                                                          |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Icon`                         | One glyph per concept on a 24 px grid (`ICON_PATHS`, `IconName`); decorative unless it gets a `label`                                                                                                                                                                                                                            |
| `StatusBadge`                  | Mower activity as word, glyph and state colour; dashed outline when offline or stale                                                                                                                                                                                                                                             |
| `Readout`                      | Labelled monospaced value with unit; flashes once when the value changes                                                                                                                                                                                                                                                         |
| `BatteryBar`                   | Ten segment battery gauge (error tone below 20 %, charging tone)                                                                                                                                                                                                                                                                 |
| `MowerCard`                    | One 72 px row of the mower list: name, connection, activity, battery, RTK, age; the whole row is a link                                                                                                                                                                                                                          |
| `CommandBar`                   | The commands passed in plus, always, the `EmergencyStop`; no prop can hide the stop, disabled actions say why                                                                                                                                                                                                                    |
| `EmergencyStop`                | Red actuator on a yellow ring; one tap sends, no confirmation, never disabled or animated                                                                                                                                                                                                                                        |
| `Can` (alias `PermissionGate`) | Renders `children` when `allowed`, otherwise `fallback` or nothing; never replaces the server check                                                                                                                                                                                                                              |
| `Button`                       | 48 px button: `primary`, `secondary`, `ghost`, `danger` (never the stop colours)                                                                                                                                                                                                                                                 |
| `Field`                        | Labelled input (text, password, e-mail, search, time, date, URL, number) with hint, error (field errors from problem+json), placeholder and `inputmode`                                                                                                                                                                          |
| `Select`                       | Labelled native select                                                                                                                                                                                                                                                                                                           |
| `Stepper`                      | Setpoint control (blade height, speed); commits 600 ms after the last step, pending style until confirmed                                                                                                                                                                                                                        |
| `SegmentedControl`             | Exclusive options in one row of 48 px segments (theme, language)                                                                                                                                                                                                                                                                 |
| `Switch`                       | An on/off setting as a 48 px row: label and hint, a square toggle that also says ON or OFF in words (`role="switch"`)                                                                                                                                                                                                            |
| `ToastRegion`                  | Toasts above the command bar, never over the stop                                                                                                                                                                                                                                                                                |
| `ProblemNotice`                | An API problem in place: title, detail, correlation id                                                                                                                                                                                                                                                                           |
| `AppShell`                     | Page frame: navigation (rail on desktop, bottom bar on phones) and content                                                                                                                                                                                                                                                       |
| `NavRail`                      | Primary navigation: icon and short label per module; an optional count badge per item (unread notifications) with an accessible text. On phones at most `phoneSlots` targets (default 5): with more items and a `moreLabel`, the rest move into a "more" sheet above the bar (see [Navigation on phones](#navigation-on-phones)) |
| `TopBar`                       | Page header: back link, title, monospaced meta line, actions; pinned under the status bar on phones (sticky)                                                                                                                                                                                                                     |
| `Panel`                        | Page section with an uppercase label on a hairline                                                                                                                                                                                                                                                                               |
| `TableScroll`                  | Sideways scroll box for a wide table with edge shades; contains its inline size, so the table never widens the page (`--md-table-scroll-bg` for the edge colour)                                                                                                                                                                 |
| `Sheet`                        | Non-modal sheet that stops above the command bar (`--md-sheet-bottom`, on desktops too when the page sets it), so the stop stays usable                                                                                                                                                                                          |
| `BottomSheet`                  | Phone sheet with three snap points (`peek`, `half`, `full`) over a map that stays visible; drag or tap the handle, Escape to peek, reports its covered height (see [Bottom sheet](#bottom-sheet))                                                                                                                                |
| `Joystick`                     | The manual drive pad (see [Joystick](#joystick)): pointer and arrow keys, `{linear, angular}` in -1..1 at a fixed cadence                                                                                                                                                                                                        |
| `MowerMap`                     | The mower map on Leaflet 1.9.4 (see [Map](#map)): areas, no-go zones, obstacles, paths, dock, mower chevron, driven route, RTK signal map, area selection, token-styled controls                                                                                                                                                 |
| `FleetMap`                     | Every mower of a fleet on one map (see [Fleet map](#fleet-map)): markers in their state colour with names, selection, the areas of loaded maps, an adaptive metric grid without tiles                                                                                                                                            |

Shared types (`Activity`, `ToastItem`, ...) are exported from `src/lib/types.ts`.

## Map

`MowerMap` (`src/lib/map/MowerMap.svelte`) draws a mower's map in the instrument panel style: areas with uppercase labels and a faint fill, no-go zones hatched in the error colour, obstacles, dashed transfer paths, the dock (a 1.2 × 0.9 m rounded footprint drawn to scale, so it grows and shrinks with the zoom, its open front edge dashed and facing the heading, with a fixed-size charging bolt on top), the mower as a chevron in its state colour (hollow and dashed when stale or offline, a dot without heading) and the driven route, fading from 30 % to full over its time span or tinted by RTK quality (fixed, float, none) with a legend. The RTK signal map bins the route points (or `rtkPoints`) into square cells in the local frame and colours each by its worst or majority fix, with a legend ("RTK-Signal": fixed, float, none, and unknown when present). Controls on the map: zoom in and out, fit, follow the mower, a layer panel (route, zones, RTK tint, RTK signal map with cell size 0.5, 1 or 2 m and worst or majority) and, when `onexpandchange` is given, full screen; all are 48 px buttons with visible focus. A scale bar in metres and a north arrow sit bottom left.

```svelte
<MowerMap
  map={snapshot.map}
  state={mowerState}
  route={positions}
  selectedAreas={picked}
  selectable={canStart}
  tiles={config.mapTiles}
  labels={mapLabels}
  areaName={(a) => a.name ?? a.id}
  onselectionchange={(ids) => (picked = ids)}
  onfail={() => (useFallback = true)}
/>
```

| Prop                                               | Meaning                                                                                                                                                                                                                                     |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `map`                                              | The `MowerMap` snapshot (local metres plus `reference`)                                                                                                                                                                                     |
| `state`                                            | The mower state (`activity`, `position` in WGS84 with compass `heading`) or null                                                                                                                                                            |
| `route`                                            | Route points `{lat, lon, observed_at, rtk_fix}` (any order)                                                                                                                                                                                 |
| `rtkPoints`                                        | Points of the RTK signal map (default: `route`), e.g. a longer time range                                                                                                                                                                   |
| `showRtk`, `rtkCellSize`, `rtkAggregate`           | Bindable state of the RTK signal map (off, 1 m, `worst` by default); the layer panel changes them too                                                                                                                                       |
| `selectedAreas`, `selectable`, `onselectionchange` | Ordered area selection; taps (and Enter or Space on a focused area) toggle                                                                                                                                                                  |
| `stale`                                            | Draws the mower hollow                                                                                                                                                                                                                      |
| `tiles`                                            | `{url, attribution, maxZoom}` switches to EPSG:3857 with raster tiles; null keeps the local grid                                                                                                                                            |
| `labels`, `areaName`                               | Every text, translated by the app (`MowerMapLabels`); the library imports no i18n runtime                                                                                                                                                   |
| `onarealabel`                                      | Each area label gets a small edit button (`area-rename-map`, `data-label-area`, named by `labels.renameArea`) that reports the area id; the label text stays click-through, so tapping an area still selects it                             |
| `onfail`                                           | Leaflet could not load or start; the app shows its fallback                                                                                                                                                                                 |
| `expanded`, `onexpandchange`                       | Full screen state, owned by the page                                                                                                                                                                                                        |
| `follow`                                           | Keep the mower centred (bindable; the drive page starts with it on, a drag or the fit control ends it)                                                                                                                                      |
| `insetBottom`                                      | Pixels covered at the bottom of the map (a `BottomSheet`'s `visibleHeight`): the fit pads by it, the scale, lower legend and attribution move above it, and the `spot` with its route preview is panned (or fitted) into the uncovered part |
| `insetTop`                                         | Pixels covered at the top left by a page overlay (the spot or selection chip): the legend moves below it, and the fit and the spot reveal keep clear of it                                                                                  |

The map container carries `data-testid="mower-map"` (configurable), `data-mode` (`local` or `geo`), `data-ready`, `data-route-points` and, with the RTK signal map on, `data-rtk-cells`; areas are `path[data-area="<id>"]`, the mower `[data-testid="mower-marker"]` with `data-tone`.

Pure modules next to it, exported from the package and unit tested without a DOM:

- `geo.ts`: `toWgs84`, `fromWgs84`, `compassHeading`, an exact port of `mowdeck_control.geo`. `scripts/geo_vectors.py` prints golden vectors from the Python implementation into `src/tests/fixtures/geo-vectors.json` (regenerate with `uv run python libs/mowdeck-ui/scripts/geo_vectors.py libs/mowdeck-ui/src/tests/fixtures/geo-vectors.json` after a change to `geo.py`); `src/tests/geo.test.ts` compares within 1e-9 degrees.
- `projection.ts`: `createProjection(reference, 'local' | 'geo')`, the adapter between local metres, WGS84 and Leaflet coordinates, plus screen rotations for headings and the north arrow. `local` uses `CRS.Simple` in metres (`[lat, lng]` is `[y, x]`); `geo` uses EPSG:3857.
- `layers.ts`: `buildLayers`, `routeSegments`, `centroid`, `toggleSelection`: snapshot, state and route to layer descriptors.
- `grid.ts`: `createGridLayer` (the local metric grid as a canvas `GridLayer`: it covers any pan and zoom, minor lines at 1, 5, 10, 50, 100 m ... never closer than 9 px, strong lines every 5th or 2nd, colour from `--md-color-line`) and the pure `gridSpacing`, `gridMultiples`.
- `rtk.ts`: `binRtk` (points to cells of a metric grid in the local frame, size and `worst` or `majority`), `cellClass`, `cellRing` (a cell's corners in map coordinates) and `rtkSummary` (shares of fixed, float, none, unknown); `RTK_CELL_SIZES`.

- `scale.ts`: `scaleFor(metresPerPx)`, the 1-2-5 scale bar of both maps.
- `fleet.ts`: the fleet map's model (see below).

Leaflet is a dependency of this package (`leaflet` 1.9.4, `@types/leaflet` 1.9.21) and is imported dynamically, so it lands in its own chunk. Its DOM rendering is covered by the Playwright gate in `tests/e2e-web`.

## Fleet map

`FleetMap` (`src/lib/map/FleetMap.svelte`) shows every mower the app passes in on one map. Mowers of different sites have different local frames, so it always uses the `geo` projection (EPSG:3857, positions in WGS84). Without tiles it sends no request to any third party and draws a metric grid around the fleet reference (the centre of the bounding box of all positions and map origins) whose step follows the visible span (1, 2 or 5 times a power of ten, every fifth line major), redrawn after every move and zoom; with `tiles` the tile layer replaces the grid. Each mower is a marker: a 48 px target with the chevron (heading) or dot in its state colour, hollow and dashed when stale or offline, and its name on a tab with a state-coloured edge. A tap or Enter selects a marker (`aria-pressed`, a ring), a tap on the map clears the selection. The areas and docks of mowers whose map snapshot is set are drawn faintly underneath (each in its mower's own frame); a control hides them.

```svelte
<FleetMap
  mowers={fleet}
  tiles={config.mapTiles}
  labels={fleetLabels}
  toneLabel={(tone) => activityLabel(tone)}
  fitKey={`${siteId}|${groupId}`}
  bind:selected
/>
```

| Prop                           | Meaning                                                                                             |
| ------------------------------ | --------------------------------------------------------------------------------------------------- |
| `mowers`                       | `FleetMower[]`: `{id, name, state, stale?, map?}` (state and map structurally as in `MowerMap`)     |
| `tiles`, `labels`, `toneLabel` | Tiles as in `MowerMap`; texts (`FleetMapLabels`) and the state word of a tone, from the app         |
| `selected`, `onselect`         | The selected mower (bindable) and a callback                                                        |
| `fitKey`                       | The map fits all markers and areas once data arrives and again whenever this changes (a new filter) |
| `areaName`, `onfail`, `testid` | As in `MowerMap` (default test id `fleet-map`)                                                      |

The container carries `data-ready`, `data-markers` and `data-areas`; markers are `[data-testid="fleet-marker"]` with `data-mower` and `data-tone`. The model in `fleet.ts` is pure and unit tested (`src/tests/fleet.test.ts`): `fleetTone`, `fleetLegend` (counts per tone in the order of `FLEET_TONES`, attention first), `unplaced` (mowers without a position, for a list beside the map), `fleetReference`, `fleetMarkers`, `fleetGeometry`, `fleetBounds`, `gridStep` and `fleetGrid`.

## Bottom sheet

`BottomSheet` (`src/lib/layout/BottomSheet.svelte`) is the phone sheet for work on the map (spot mowing): non-modal, without a backdrop, so the map above it stays visible and tappable. It stops above the command bar (`--md-sheet-bottom`) and is at most 720 px wide.

| Snap   | Height                                                                                                  | Content                                                 |
| ------ | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| `peek` | The measured handle plus the `peek` snippet (about 140 to 180 px; at least 140)                         | A summary and the primary action; visible at every snap |
| `half` | 45 % of the space above the sheet's bottom edge                                                         | The `peek` snippet plus the top of the body             |
| `full` | 90 % of that space, but always 56 px less than all of it, so the map never disappears; the body scrolls | Everything                                              |

- **Handle.** A full-width button (32 px) with a three-step level meter of square pips. Drag it with a pointer (pointer capture, `touch-action: none`): the sheet follows the finger between its lowest and highest allowed snap; on release a fling faster than 0.5 px/ms goes on to the next snap in its direction, a slower release to the nearest snap. A tap, Enter or Space cycles peek, half, full. It carries `aria-expanded` (not at peek) and `aria-controls` (the body). Escape anywhere returns to peek.
- **Props.** `open`, `snap` (bindable), `snaps` (the allowed ones, peek always; for example `['peek']` while a run must keep its STOP in view), `label`, `handleLabel`, `visibleHeight` (bindable, read only: the settled snap height plus the safe-area inset, 0 when closed; also `data-visible-height`), `safeArea` (default on: adds `env(safe-area-inset-bottom)` for a sheet on the screen edge, as in the Capacitor shell; off above a bar that adds it itself), `peek` and `children` snippets, `onsnapchange`, `testid` (the handle gets `<testid>-handle`, the body `<testid>-body`). The body is `hidden` at peek, so its controls leave the tab order.
- **Motion.** The height animates with `--md-motion-base`; none while dragging and none with `prefers-reduced-motion`.
- **Map.** Pass `visibleHeight` to `MowerMap` as `insetBottom`: the fit pads by it, the scale and attribution move above it, and the spot with its route preview is panned into the uncovered part (fitted when larger; `viewport.ts`: `pixelBox`, `revealPan`).
- **Logic** (`bottom-sheet.ts`, unit tested in `src/tests/bottom-sheet.test.ts`): `snapHeights`, `allowedSnaps`, `coerceSnap`, `nextSnap`, `clampHeight`, `nearestSnap`, `releaseSnap`, `releaseVelocity` (pointer samples of the last 100 ms). The component is tested in `src/tests/BottomSheet.test.ts`.

## Navigation on phones

The bottom bar keeps every target at least a fifth of a 390 px screen wide and 64 px tall, so it holds five. Past five modules, `NavRail` with a `moreLabel` shows the first four and a "more" button (three dots); the button opens a sheet above the bar with the remaining modules as 56 px rows (icon, label, unread count). The sheet closes on a choice, Escape, a tap outside or any navigation; the more button is marked current (the bar's top line) while one of its modules is open, and carries a dot when one of them has a badge. The desktop rail shows every module. Five or more targets switch the labels to plain case, so German labels fit.

## Joystick

`Joystick` (`src/lib/drive/Joystick.svelte`) is the pad of the manual drive page: a round, high-contrast pad (`min(78vw, 300px)`, overridable with `--md-stick-size`) with a 72 px thumb, rings, axes and direction marks, the dead zone drawn in the centre and the deflection as a line. It never sends anything; the page turns samples into drive frames.

```svelte
<Joystick
  label={m.drive_pad()}
  hint={m.drive_pad_hint()}
  disabled={!live}
  interval={1000 / session.maxRateHz}
  bind:value={stick}
  onsample={(v) => pilot.sample(v)}
  onengage={(held) => !held && pilot.release()}
/>
```

| Prop                   | Meaning                                                                                              |
| ---------------------- | ---------------------------------------------------------------------------------------------------- |
| `label`, `hint`        | Accessible name and description (texts from the app)                                                 |
| `disabled`             | No input; becoming disabled while held releases the pad (dashed outline, muted thumb)                |
| `deadZone`             | Radial dead zone as a fraction of the radius (default 0.1); outside it the output is rescaled from 0 |
| `interval`             | Milliseconds between `onsample` calls while held (default 100); the first sample comes at once       |
| `value`                | Bindable current vector                                                                              |
| `onsample`, `onengage` | Samples while held; `onengage(true)` on press, `onengage(false)` on release, Escape, blur or disable |

Up is forward (`linear` +1), left turns left (`angular` +1), as in the `ManualDrive` contract. Pointer events with pointer capture work for touch and mouse (`touch-action: none`); the arrow keys drive when the pad has focus (diagonals normalised). The pad is `role="application"` with `aria-roledescription="joystick"` and carries `data-engaged`, `data-linear` and `data-angular` for tests. The math is pure in `src/lib/drive/stick.ts` (`stickVector`, `keysVector`, `clampToPad`, `limitVector` for the speed limit, `isZero`), exported and unit tested.

## Adding a component

1. Put it in the folder of its concern under `src/lib/` (`status`, `mower`, `command`, `permission`, `form`, `feedback`, `layout`, `icons`, `map`, `drive`), with a short comment at the top that says what it is for.
2. Texts and decisions are props; no fetch, no stores, no Paraglide. Use only tokens; add a token to `tokens.css` (both themes) when one is missing and run `tokens:check`.
3. Export it from `src/lib/index.ts`.
4. Add a Vitest test in `src/tests/` (Testing Library): every state, the accessible names, and for anything near the command bar that the emergency stop stays rendered.
5. Run `pnpm --filter @mowdeck/ui lint`, `check` and `test`.

## Checks and lint rules

| Command                                  | What it does                                  |
| ---------------------------------------- | --------------------------------------------- |
| `pnpm --filter @mowdeck/ui lint`         | ESLint, Prettier check, Stylelint             |
| `pnpm --filter @mowdeck/ui check`        | `svelte-check --fail-on-warnings`             |
| `pnpm --filter @mowdeck/ui test`         | Vitest with coverage, then the contrast check |
| `pnpm --filter @mowdeck/ui tokens:check` | Contrast check only                           |

The library also ships the lint rules of the frontend workspace (`lint/`):

- `stylelint-tokens.js`: Stylelint rule `mowdeck/use-tokens`. Raw colours (hex, `rgb()`, named colours), font stacks and radii are allowed only in `tokens.css`.
- `eslint-no-literal-text.js`: ESLint rule `mowdeck/no-literal-text`, enabled for `apps/web/src/**/*.svelte`. Text nodes and `aria-label`, `title`, `placeholder` and `alt` values with letters must come from the messages (units like `mm`, `m/s`, `%` are allowed).
- `contrast-check.js` (`tokens:check`): computes the WCAG contrast of the token pairs in both themes and fails below the target: text 4.5:1 (primary ink 7:1 in light, the outdoor theme), state marks and controls 3:1 against the surface, each state 4.5:1 on its tint, stop ink 4.5:1 on the stop fill, inverse ink 4.5:1 on the paused and error fills (labels on a solid state chip), and the theme-independent HUD inks (`--md-hud-ink-inverse` on `--md-hud-ink`, `--md-hud-lit` on the HUD plate) 4.5:1.

## Pull request checklist (UI)

For every change to this library or `apps/web`, with light and dark screenshots at phone and desktop width (`pnpm --filter e2e-web screens`):

- [ ] Only tokens used; no new colours, radii or fonts outside `tokens.css`
- [ ] Colour is used only for state; every state also has text or a glyph
- [ ] Readouts in Plex Mono with units; labels uppercase 12 px
- [ ] Targets at least 48 px, gaps at least 8 px; checked at 390 px width
- [ ] Emergency stop visible and reachable on every mower view, including with sheets and keyboard open
- [ ] Light and dark screenshots attached; sunlight check (light theme, full brightness) for new status UI
- [ ] No hard-coded strings; German and English texts present and fitting (German is longer)
- [ ] Pending, error, empty, offline and no-permission states designed
- [ ] Keyboard focus visible, `prefers-reduced-motion` respected
