# Licensing

| Part | License |
| --- | --- |
| Everything by default: `services/*`, `apps/*`, `tools/*`, `infra/*`, `bridge/*`, `integrations/*`, docs | [AGPL-3.0](LICENSES/AGPL-3.0-only.txt) (the root `LICENSE`) |
| Reusable libraries: `libs/*` | [Apache-2.0](LICENSES/Apache-2.0.txt) |
| Exception: `libs/mowdeck-control-mammotion` (imports the GPL-3.0 library PyMammotion) | [GPL-3.0](LICENSES/GPL-3.0-only.txt) |
| Exception: `libs/mowdeck-control-worx` (imports pyworxcloud, whose sdist ships a GPL-3.0 `LICENSE`) | [GPL-3.0](LICENSES/GPL-3.0-only.txt) |
| Exception: `integrations/agora-bridge` (the camera relay's optional Agora source; its own `LICENSE`) | [Apache-2.0](LICENSES/Apache-2.0.txt) |

Every file names its licence and copyright in machine-readable form ([REUSE](https://reuse.software/)): source files carry `SPDX-License-Identifier` and `SPDX-FileCopyrightText` tags in their first lines, files without comments are listed in [`REUSE.toml`](REUSE.toml), and the licence texts are in `LICENSES/` under their SPDX identifiers. CI runs `reuse lint` on every pull request.

Each library under `libs/` ships its own `LICENSE` file and declares the license in its `pyproject.toml`. Libraries are Apache-2.0 unless a GPL dependency forces GPL-3.0; that is the case for the PyMammotion adapter package, see [ADR 0011](docs/adr/0011-gpl-package-for-pymammotion-adapter.md), and for the pyworxcloud adapter package ([ADR 0066](docs/adr/0066-cloud-provider-plugins-and-more-vendors.md)). `libs/mowdeck-control` itself never imports either library.

Why: the services and apps are a network service, so AGPL-3.0 ensures that modified hosted versions stay open. The libraries (control, contracts, auth and so on) are meant to be reused by other projects without copyleft obligations. See [ADR 0005](docs/adr/0005-licensing.md).

## Source code for network users (AGPL-3.0 section 13)

Everyone who uses a MowDeck installation over a network gets the source code of the version it runs:

- the sign-in and registration pages show the version, `AGPL-3.0` and a "Source code" link, so no account is needed;
- Settings, About shows the version, the licence and the full address of the source of this build;
- every HTTP response of every service carries `X-Source-Code: <address>` (set by `libs/mowdeck-service-kit`).

The address is the repository at the tag of the release (`https://github.com/lukislp/mowdeck/tree/v<version>`), at the commit of a test build, or the repository itself for a local build. The release build bakes `MOWDECK_VERSION`, `MOWDECK_SOURCE_REF` and `MOWDECK_SOURCE_URL` into every image.

**Forks:** whoever runs modified code must offer that code. Publish it, point `SOURCE_URL` (Compose) or `config.SOURCE_URL` (Helm) or the build argument `MOWDECK_SOURCE_URL` to your public repository, and build from a tag or commit that exists there; do not remove the link or the header. Details: [docs/ops/source-code.md](docs/ops/source-code.md).

## The native apps

The iOS and Android apps (`apps/mobile`) contain only: the bundled pages (first-launch screen, offline page and About page, built from `apps/mobile/src`) and the Swift and Java shell, AGPL-3.0-only like the rest of `apps/`; the stylesheets of `libs/mowdeck-ui` (Apache-2.0) with the IBM Plex fonts (SIL OFL 1.1, text included); Capacitor and its plugins (MIT) and `tslib` (0BSD). The web app is not bundled; it is loaded from the user's own server, so its licence and source offer are shown by that server. The app's About page shows the version, the licence and the source link of the app's version, and the notices of the third-party code, generated from the packages by `tools/repo-checks/scripts/app_notices.py`; GPL, AGPL and LGPL code is refused there. Details: [docs/ops/mobile-apps.md](docs/ops/mobile-apps.md#about-licences-and-third-party-notices). This section only describes the bundle. Whether the App Store's terms need an additional permission for distributing AGPL code is a decision of the maintainer; none has been granted.

## Third-party components

Third-party components (Traefik, NATS, PostgreSQL, OpenFGA and so on) keep their own licenses; they are used as unmodified container images.

The Agora SDK (proprietary) is never part of this repository or of a published image: `integrations/agora-bridge` downloads it while the operator builds that image, which means accepting Agora's terms. It is not part of MowDeck and has to be built by each operator; Agora's terms exclude GPLv3/LGPLv3/AGPLv3 code from the applications that use the SDK, which is why the bridge is a separate Apache-2.0 program ([ADR 0045](docs/adr/0045-camera-relay.md), [agora-bridge README](integrations/agora-bridge/README.md)).

The Agora Web SDK (`agora-rtc-sdk-ng`, closed source; used by the web app's direct camera path, ADR 0032) is likewise never part of a published image: the release builds the web app without it (`WEB_AGORA_SDK=exclude`, [apps/web](apps/web/README.md#agora-web-sdk-and-third-party-licences)); the capture worker image `vision-capture`, which downloaded it and was never published, is removed. What each published image contains and declares: [docs/ops/release.md](docs/ops/release.md#licences).
