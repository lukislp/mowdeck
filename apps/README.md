# apps

User-facing applications. They are packages of the pnpm workspace (`pnpm-workspace.yaml`), not of the uv workspace.

| App | Purpose |
| --- | --- |
| [`web`](web/README.md) | The web app: SvelteKit 2 single-page PWA, served by the Compose service `web` at `/` (AGPL-3.0) |
| [`mobile`](mobile/README.md) | iOS and Android app shells (Capacitor) around the web app served by the MowDeck server (AGPL-3.0) |

Developer guide: [docs/dev/web-app.md](../docs/dev/web-app.md).
