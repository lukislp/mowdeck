# ADR 0062: Setup tour for new users

## Status

Accepted (2026-10-28)

## Context

The owner wants a guided setup for new users ("Onboarding-Tour: geführte Einrichtung für neue Nutzer"): after the first sign-in, and again on request, walk the user to a working MowDeck. Connect the Mammotion account and add the mower, check that the map arrived, set up notifications, add a passkey, optionally link a sign-in, create the first schedule, and inside the iPhone app turn on the widgets and the Apple Watch. Each step shows whether it is done, can be skipped, and the tour can be resumed. Short "first time here" hints on the main screens (mower map, schedules) disappear once dismissed.

Forces:

- Every step already has its flow: the linked accounts section ([ADR 0034](0034-linked-cloud-accounts-and-device-discovery.md)), `/mowers/new`, the mower page with its map, the notification settings with Web Push, APNs and ntfy, the passkeys panel ([ADR 0050](0050-passkeys-and-app-login-hand-off.md)), the provider links ([ADR 0051](0051-sign-in-with-github.md), [ADR 0052](0052-sign-in-with-apple.md)), `/schedules/new`, the widgets panel of the app ([ADR 0059](0059-ios-widgets-and-live-activity.md), [ADR 0060](0060-apple-watch-app.md)). The tour must link them, not duplicate them (the install hint for iOS browsers stays where it is).
- "Done" must come from the real state, not from a checkbox the user ticks: a mower exists, Telemetry has its map, a push channel is registered, a passkey exists, a schedule exists.
- The progress should follow the user from the browser to the iPhone app. Identity had no per-user preferences so far; the web app keeps device-local flags (`mowdeck.whats-new-seen`, the install hint) in `localStorage`.
- Existing users (the owner) must not be bombarded: their accounts are set up already.
- Many e2e specs register fresh accounts and drive the mower list; a modal tour on top of it would block them and the user alike.

## Decision

### What is stored, and where

Identity keeps **only the user's own choices**, per user, in a new table `onboarding_progress` (migration 0006, one row per user, removed with the user): `status` (`active`, `dismissed`, `completed`; no row means `new`), `skipped_steps` and `dismissed_hints` (JSON lists of keys). `GET` and `PUT /api/v1/identity/me/onboarding` read and replace it; only users and sub-users have a tour (API tokens 403). The keys belong to the web app: Identity checks only their shape (`^[a-z][a-z0-9-]{0,31}$`, at most 32 each), so a new step needs no server change. The web app keeps keys it does not know when it saves (another app version may have written them). No audit entries: these are display preferences.

Server side rather than `localStorage`, because the tour is about the account, not the device: a user who skips the passkey step in the browser should not be asked again in the iPhone app, and a dismissed hint should stay dismissed everywhere. A dedicated table rather than a generic preferences store: there is exactly one consumer, and a typed table keeps the contract (and its validation) explicit; a generic store can absorb it later.

**Whether a step is done is never stored.** The web app reads it every time from the services:

| Step | Applies when | Done when |
| --- | --- | --- |
| `cloud-account` Mammotion account | the user has `fleet.manage` and a connection kind takes mowers from a linked account | Fleet lists a linked account, or the user sees a mower (a mower of another kind needs no cloud account) |
| `mower` Add the mower | `fleet.manage` | Fleet lists a mower |
| `map` Check the map | always | Telemetry answers 200 for the map of one of the first three mowers |
| `notifications` | the notification settings are visible | a Web Push subscription, an iOS (APNs) device or an ntfy target exists |
| `passkey` (optional) | passkeys are configured (`/login-methods`) | the user has a passkey |
| `schedule` | the Scheduler is deployed and visible | a schedule exists |
| `sign-in-links` (optional) | GitHub or Apple sign-in is configured | a provider is linked |
| `app-extras` (optional) | inside the iOS app with widget support | the widgets are on on this iPhone |

A fact that cannot be read is unknown: the step stays open, it is never counted as done. The tour is complete when every required step is done or skipped; optional steps never hold it up.

### Who sees what

- **A new account that may add mowers and has none** (status `new`, no mower, `fleet.manage`): the mower list shows the setup card, and the tour starts by itself (status `active` is stored on the first visit).
- **An account set up before the tour existed** (status `new`, at least one mower): one slim, dismissible line ("Einrichtungshilfe: prüfen …") on the mower list; dismissing it stores `dismissed`. Nothing else.
- **`dismissed` / `completed`**: nothing on the mower list. Settings → Einrichtung (for every user) shows the status and opens the tour again; opening it on purpose resumes a dismissed tour.
- **First-time hints** (`mower-map`, `schedules`) show only to users who started the tour (`active` or `completed`), each until dismissed. Accounts set up before the tour never see them.
- "Was ist neu" is unchanged: the card sits in the page flow at the top and the sheet is non-modal, so closing the sheet leaves the card as it was.

### UI

- The setup card sits **in the page flow** of the mower list (like the install hint), never as an overlay: the list, the add button and the emergency stop stay usable. It shows the progress scale (one segment per step: filled done, hatched skipped, hairline open, the count in mono), the next open step with its action into the existing flow, "Tour öffnen" and "Später".
- `/setup` is the tour itself: a numbered checklist with the state of every step; the next open step is expanded (what it is for, its action, "Überspringen"), a skipped one offers "Wieder aufnehmen". The facts are read again when the page comes back into view (after a flow in another tab or app). When the last required step settles, a completion panel takes the focus; "Fertig" stores `completed`.
- Accessibility: steps are disclosure buttons with `aria-expanded` and `aria-controls` inside headings; after a skip the focus moves to the next open step; the progress scale is announced as a sentence ("3 von 7 erledigt"), its segments are hidden from screen readers; motion is limited to short fades and off with `prefers-reduced-motion`.
- Mobile-first: the checklist is one column on phones, the panel takes the full width under its step; targets are at least 48 px.

## Consequences

- One small table and two endpoints in Identity; the web app reads the facts with up to about ten small requests when the tour or the mower list card needs them (not for dismissed or completed tours, which only load the choices).
- A new step is a web app change only (definition, texts, a probe). A step key removed from the web app stays harmlessly in the stored lists.
- The e2e suite covers a new account, skip, resume in another browser, completion from the real state, the first-time hints and the one-line hint for an account set up before the tour (`tests/e2e-web/tests/onboarding-desktop.spec.ts`).
- Not covered: the native-only step is tested at the unit level (the e2e suite runs in browsers); facts that change in another tab are picked up when the tour page comes back into view, not live.

## Alternatives considered

- **`localStorage` only** (like "Was ist neu"): no server change, but the tour would restart on every new device and in the iPhone app, and hints would come back after clearing site data.
- **Store "done" per step on the server**: stale as soon as something changes elsewhere (a mower deleted, a passkey removed), and a second source of truth next to the services.
- **A modal tour over the app** (coach marks): blocks the very flows it points to and every e2e spec that starts on an empty mower list; a page plus an in-flow card is calmer and works the same with a screen reader.
- **Show the tour to every account without a stored status**: would bombard existing, set-up accounts; they get one dismissible line instead.
