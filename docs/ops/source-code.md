# Source code offer (AGPL-3.0 section 13)

MowDeck's services and web app are licensed under the AGPL-3.0 ([LICENSING.md](https://github.com/lukislp/mowdeck/blob/main/LICENSING.md)). Section 13 requires that everyone who interacts with an installation over a network is offered the corresponding source code of the version it runs. MowDeck makes that offer in three places, all built from the same three values:

| Where | What |
| --- | --- |
| Sign-in and registration pages | A row under the form: the version (`v0.3.0`, or `dev` for a local build), `AGPL-3.0` and the link "Quellcode" / "Source code". Reachable without an account. |
| Settings, About | The last section for every user: version, licence (with the licence text) and the full address of the source of this build. |
| Every HTTP response of a service | The header `X-Source-Code: <address>` (set by `mowdeck-service-kit` for all services, errors included). |

## The values

| Variable | Set by | Meaning |
| --- | --- | --- |
| `MOWDECK_VERSION` | the image build (`--build-arg`) | Version of the build; empty for a local build |
| `MOWDECK_SOURCE_REF` | the image build | Git ref of the build: the tag `v<version>` of a release, the commit of a test build. Empty: `v<version>` when the version is a semantic version |
| `MOWDECK_SOURCE_URL` | the image build, overridable at runtime | Repository of the running code; default `https://github.com/lukislp/mowdeck` |

Every MowDeck image (`services/*/Dockerfile`, `apps/web/Dockerfile`) takes the three as build arguments and keeps them as environment variables. The services read them through `ServiceSettings` (`mowdeck_service_kit.source`); the web container renders them into `/config.json` at start (`apps/web/nginx/40-mowdeck-runtime-config.sh`), where the app reads them.

The address is `<MOWDECK_SOURCE_URL>/tree/<ref>`, for example `https://github.com/lukislp/mowdeck/tree/v0.3.0`; without a ref (a local build) it is the repository itself. GitHub understands `/tree/<ref>`; for another host put the placeholder `{ref}` into the URL, for example `https://codeberg.org/me/mowdeck/src/tag/{ref}` (without a ref it becomes `HEAD`). An invalid URL (not http(s), or with whitespace, quotes or angle brackets) falls back to the official repository.

## Release builds

The release workflow passes, for every image:

```text
MOWDECK_VERSION=<version>                     # e.g. 0.3.0
MOWDECK_SOURCE_REF=v<version>                 # the signed tag; a test build without a tag: the commit SHA
MOWDECK_SOURCE_URL=<server_url>/<repository>  # ${{ github.server_url }}/${{ github.repository }}
```

`MOWDECK_SOURCE_URL` from the workflow's own repository means a fork that builds its images with the same workflow automatically points to itself. Images built without these arguments (Compose `--build`, a local `docker build`) show `dev` and link the repository root.

## Configuration

- **Compose:** `SOURCE_URL` in `infra/compose/.env` (default the official repository) goes to every service and the web app as `MOWDECK_SOURCE_URL`.
- **Helm:** `config.SOURCE_URL`, empty by default, which keeps the value baked into the images (the repository they were built from). Set it when the running images come from another repository than the one they name.

## Forks and modified installations

If you run modified code, the offer must lead to **your** source of exactly the version you run:

1. Publish your changes in a public repository (all of it, including the build files).
2. Point `SOURCE_URL` (Compose) or `config.SOURCE_URL` (Helm) to it, or build your images with `--build-arg MOWDECK_SOURCE_URL=<your repository>`.
3. Build your images from a tag or commit that exists in that repository and pass it as `MOWDECK_SOURCE_REF` (and the version as `MOWDECK_VERSION`), so the link opens exactly that code.
4. Check: the sign-in page shows your version and its link opens your code; `curl -sI https://<host>/api/v1/identity/session | grep -i x-source-code` names the same address.

Do not remove the link or the header; they are how the installation meets section 13.
