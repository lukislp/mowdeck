# ADR 0003: SvelteKit for the web app

## Status

Accepted (2026-10-08)

## Context

The web app must be an installable PWA that works on phones and desktops, shows a live map and status, and is small enough to be served as static files (CDN-capable). The plan left the choice open between SvelteKit and React with Vite.

## Decision

Use SvelteKit (TypeScript) with the static adapter, packaged as a PWA. The map uses Leaflet with Geoman. The app is organized as a shell with modules (route, menu entry, required permission). The UI is German by default with English available. Capacitor can later wrap the same PWA for stores.

## Consequences

- Small bundles and simple reactive code suit a PWA on phones.
- Smaller ecosystem and hiring pool than React; accepted for this project.
- Shared UI building blocks live in a `mowdeck-ui` library.
- The frontend is the only non-Python component of the main stack.
