# ADR 0073: Guided tour with coach marks

## Status

Accepted (2026-11-01). Extends [ADR 0062](0062-setup-tour.md) (setup tour) and revisits its rejected alternative "a modal tour over the app".

## Context

The owner wants an interactive, step-by-step tour of the web app: short notes that point at the real controls (spotlight and coach marks, next/back/skip, progress) for the core of MowDeck: the mower list, the mower page (live map, start/pause/dock, spot mowing), schedules, zones, notifications and settings. It starts by itself once for a new user, can be started again any time, works in the public demo ([ADR 0068](0068-public-demo-mode.md)) and on phones and desktops, and is fully keyboard and screen reader accessible.

What exists:

- The **setup tour** of ADR 0062 is a checklist of things to *do* (link the account, add the mower, push, passkey, first schedule), shown as an in-flow card on the mower list and the `/setup` page. It deliberately rejected "a modal tour over the app (coach marks)" because such a tour "blocks the very flows it points to and every e2e spec that starts on an empty mower list".
- Identity stores the setup tour's choices per user (`GET`/`PUT /api/v1/identity/me/onboarding`): a status, skipped steps and `dismissed_hints`, a list of opaque keys the web app owns (`^[a-z][a-z0-9-]{0,31}$`, at most 32). A new key needs no server change.
- Features are gated by the mower's capabilities (`GET /telemetry/mowers/{id}/capabilities`, ADR 0012), by permissions and by the modules the deployed services announce (the shell registry).
- The demo shares one user between every visitor and locks what the services refuse (`DemoLock`).

The two tours answer different questions: the setup tour "what is still missing?", the guided tour "where is what?". The guided tour shows; it does not set anything up.

## Decision

### A small in-house component, no tour library

`apps/web/src/lib/tour/`: `steps.ts` (pure: the steps, the filters, moving over missed steps, the automatic start rule, the outcome keys), `tour.svelte.ts` (the state machine; navigation, DOM lookup, storage and services injected through `TourDeps`), `runtime.ts` (the wiring), `TourOverlay.svelte` (the overlay) and `labels.ts` (texts). It is mounted once in the `(app)` layout next to "What's new".

Libraries considered: driver.js (MIT, small, framework free) would do the highlighting, but its popover markup and styles would be replaced anyway to fit the instrument panel design system, its focus handling would have to be extended for a focus trap and live announcements, and steps that live on other pages, wait for their element or are filtered by capability are not its model; intro.js is AGPL-3.0 with a paid commercial licence and brings its own look. Everything the tour needs (one fixed overlay, one rectangle, one card, a step machine) is a few hundred lines that use the existing tokens, `Button` and Paraglide; no new dependency.

### Steps, filtered by what is there

| Step | Page | Points at (`data-tour`) | Shown when |
| --- | --- | --- | --- |
| `welcome` | where the user is | nothing (centred card) | always |
| `overview` | `/mowers` | the mower list | the user sees a mower |
| `map` | `/mowers/{first}` | the map section | the user sees a mower |
| `commands` | `/mowers/{first}` | the command bar with the emergency stop | the user sees a mower |
| `spot` | `/mowers/{first}` | "Spot mähen" | a mower whose capabilities have `manual_drive` (spot runs drive the mower, ADR 0039) |
| `zones` | `/mowers/{first}` | the areas list, or the zone editor link (ADR 0070) | the user sees a mower |
| `schedules` | `/schedules` | the schedule list or its empty state | a mower, and the `schedules` module is visible |
| `inbox` | `/notifications` | the inbox or its empty state | a mower, and the `notifications` module is visible |
| `settings` | `/settings` | the numbered settings index | the user sees a mower |
| `restart` | `/settings#setup` | "Start the tour" | always |
| `add-mower` | `/mowers` | the empty mower list | the user sees no mower: the last step, its button leads to `/mowers/new` (for a user with `fleet.manage`; a viewer reads that a mower will appear) |

Filters are declared on the step (`applies(context)`) and read from the provider neutral sources: the first mower's capabilities, `fleet.manage`, the visible modules. Nothing names a vendor. The mower steps use the first mower of the list.

**A missing element never blocks.** A step's element is looked up for up to 6 s after the page opened (`[data-tour="..."]`, first visible match). If it does not appear (the page failed to load, a mower without areas or zones, a layout that leaves it out), the step is marked missed for this run and the tour moves on in the direction of travel; Back passes over it too, and the progress counts only the steps still shown. If the element leaves the page while shown, it is looked up again.

### Starting by itself, once

The tour starts by itself only on `/mowers` (the landing page after the sign-in, never over a deep link), not while "What's new" is open, once per session, and only when:

- the stored outcome is `new` (never seen), **and**
- the user is new in the sense of ADR 0062: the setup tour's status is `active`, or `new` with no mower. An account set up before the tours existed is not bombarded; Settings → Setup starts it.

In the demo every new **browser** is a new visitor (below).

### What is stored where

| What | Where | Why |
| --- | --- | --- |
| Outcome: `tour-seen` (opened, not finished), `tour-done`, `tour-skipped` | Identity, the user's `dismissed_hints` (ADR 0062) | Per user, follows them to every device and the app shells; existing table and endpoint, no server change. The web app replaces only its own keys and keeps unknown ones. |
| Outcome in the demo | `localStorage` `mowdeck.tour.demo` | Every visitor shares the demo user: a server-side outcome would let the first visitor end the tour for everyone. |
| The running step | `sessionStorage` `mowdeck.tour.run` (with the user's subject) | A reload in the middle continues the tour in that tab; nothing to keep beyond it. |
| Device switch | `localStorage` `mowdeck.tour.autostart` = `off` | Never start by itself on this device: wall displays, kiosks and the e2e suite. Starting it from Settings still works. |

`tour-seen` is written when the tour opens, so a tour closed with the tab does not start again. Finishing (`done`) also dismisses the setup tour's first-time hints on the mower map and the schedules (the tour explained them). A setup tour status `new` is saved as what the mower list would make of it (`active` for a new account that may add mowers and has none, otherwise `dismissed`), because Identity has no `new` to store; this hides the one-line "check the setup" hint of an old account that takes the guided tour, which is the same answer. Taking the tour again never turns `done` into `skipped`.

### The overlay

- **Viewfinder, not a glow.** The framed element sits in a hole of the dimmed page (`--md-scrim`), marked with four corner brackets in HUD ink and the step number as a mono tag, like a callout on an instrument drawing. The coach card is a plain surface with the ink top rule of the sheets, an uppercase label, a mono counter ("03 / 10") and a tick scale (past, now, ahead), the step's title and text, and in the demo a note with the demo band's hatched tape. Tokens only, light and dark from the same tokens.
- **Placement.** Below 768 px the card docks to the top or bottom edge, whichever is away from the framed element (the command bar at the bottom gets the card at the top). Wider, it sits below or above the element, beside a screen-tall one, clamped into the viewport.
- **Never moves the page.** Fixed layers only, no scroll lock, no inserted elements in the flow: no layout shift. The element is scrolled into view once (`smooth`, `auto` with reduced motion).
- **Nothing is triggered.** A transparent layer catches every click on the page while the tour runs, the framed element included. In the demo the notes explain what is locked; the tour itself never sends a command or opens a locked flow.
- **Keyboard and screen readers.** A `role="dialog"` with `aria-modal`, labelled by the step title and described by its text; the focus moves into the card (the primary button) and stays there (Tab wraps, focus that leaves is brought back); `→` next, `←` back, `Esc` leaves the tour (stored as skipped); a polite live region announces "Step 3 of 10: The live map"; the counter's visual form is hidden from screen readers in favour of the sentence; on closing the focus returns where it was. Targets are 48 px.
- **Motion.** The frame glides between targets and the card rises once (`--md-motion-base`); both off with `prefers-reduced-motion`.

### Where to start it again

Settings → Setup (`#setup`, for every user) has a "Guided tour" panel with the outcome and "Start the tour", above the setup tour's panel. The tour's own `restart` step points there.

### Texts

All texts are Paraglide messages (`tour_*`) in German and English; the i18n check covers them. Step texts are short (one or two sentences), German first.

## Consequences

- No new dependency, no server change: Identity already stores opaque keys. A new step is a definition in `steps.ts`, two texts and a `data-tour` attribute on the page.
- Pages carry `data-tour` attributes on the elements the tour points at (`overview`, `add-mower`, `map`, `commands`, `spot`, `zones`, `schedules`, `inbox`, `settings`, `tour-restart`). Removing one does not break the tour (the step is passed over) but the e2e specs notice.
- The e2e suite runs with the device switch off for every context (`storageState` in `playwright.config.ts`, which Playwright also gives to `browser.newContext()`), so specs that sign in fresh accounts are not covered by a modal tour. The tour specs start with a clean browser: `tour-desktop.spec.ts` (automatic start once, leading to "add mower", Esc stored as skipped, start from Settings, arrow keys over the mower screens with the focus kept in the card, a step whose page fails to load passed over in both directions), `tour-phone.spec.ts` (390 px: docking away from the target, no sideways overflow, Esc), `tour-demo.spec.ts` (projects `demo` and `demo-phone`: first visit of a browser, the demo notes, a second browser gets it again). Unit tests: `steps.test.ts` (filters with property tests, moving over missed steps, outcome keys, the automatic start rule), `tour.svelte.test.ts` (the state machine with fakes) and `tour-overlay.test.ts` (dialog semantics, keys, focus trap, demo notes, the last step).
- The tour reads the mower list and one capability set when it starts (two small requests); the automatic start also loads the setup tour's choices, which the mower list loads anyway.
- Not covered: a mower page that the user changes during the tour (the tour shows the first mower); steps for the iOS app's native extras (ADR 0062's setup tour covers them).

## Alternatives considered

- **A tour library** (driver.js, intro.js): see above; restyling and extending one costs about as much as the component, and adds a dependency to keep up to date, or a licence that does not fit.
- **`localStorage` only** for the outcome: no request, but the tour would start again on every device and in the app shells; kept only for the demo, where the user is shared.
- **Non-modal coach marks** (the page stays clickable): would let a click on a highlighted command start the mower in the middle of a tour, and leave keyboard and screen reader users without a clear context. The modal overlay with Esc always one key away, and the e2e device switch, answer ADR 0062's concerns instead.
- **Start for every account without a stored outcome**: would start a tour over the owner's long set-up installation after an update; ADR 0062's rule for "new" is reused instead.
