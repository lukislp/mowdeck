# Releases

A release publishes the container images, the Helm chart and a GitHub release from one tag. The workflow is `.github/workflows/release.yml`; the decisions are in [ADR 0029](../adr/0029-helm-chart-and-release-pipeline.md).

## Cutting a release

1. `main` is green (CI including `e2e`, `e2e-web` and the `helm` chart checks). Optionally run the kind install test first: `gh workflow run helm-kind.yml --ref main`.
2. Update `docs/phases.md` if the release closes a task or gate.
3. Tag and push (semantic version, `v` prefix):

   ```bash
   git tag -a v0.1.0 -m "MowDeck 0.1.0"
   git push origin v0.1.0
   ```

The tag starts `release.yml` and `helm-kind.yml`. A tag with a pre-release suffix (`v0.2.0-rc.1`) is published as a pre-release and does not move `:latest`.

**Before the first release:** if packages with these names (`mowdeck/<image>`, `charts/mowdeck`) already exist under the owner on GHCR, grant this repository write access to them or remove them before the first release; otherwise the push is refused.

**Test builds without a tag:** `gh workflow run release.yml -f version=0.1.0-rc.1` builds and pushes the images and the chart with that version but creates no GitHub release (`-f publish-release=true` creates one; `:latest` moves only for a final version).

## What the workflow publishes

| Job | Output |
| --- | --- |
| `version` | the version from the tag (`v0.1.0` gives `0.1.0`) or the input, checked against semantic versioning |
| `images` (one job per image, in parallel) | `identity`, `fleet`, `device-gateway`, `telemetry`, `realtime`, `notifications`, `scheduler`, `entitlements`, `planner`, `web` and `authz-bootstrap` for **linux/amd64 and linux/arm64** as one multi-platform image each: `ghcr.io/<owner>/mowdeck/<image>:<version>`, `:v<version>` and, for a final release, `:latest`. Built with Buildx, QEMU for arm64 ([docker/setup-qemu-action](https://github.com/docker/setup-qemu-action), [multi-platform builds](https://docs.docker.com/build/building/multi-platform/)) and the GitHub Actions cache ([gha cache backend](https://docs.docker.com/build/cache/backends/gha/)); base images as pinned in `infra/compose/.env.example`. Each image carries an SBOM and a provenance attestation ([build attestations](https://docs.docker.com/build/metadata/attestations/sbom/)) and gets an SPDX SBOM file from [anchore/sbom-action](https://github.com/anchore/sbom-action). |
| `chart` | `helm package` with `version` and `appVersion` set to the release version (so `image.tag` defaults to the released images), pushed as an OCI artifact to `oci://ghcr.io/<owner>/charts/mowdeck:<version>` ([Helm registries](https://helm.sh/docs/topics/registries/)) |
| `release` | a GitHub release `v<version>` with notes generated from the merged pull requests and commits since the previous tag ([generated release notes](https://docs.github.com/en/repositories/releasing-projects-on-github/automatically-generated-release-notes), [softprops/action-gh-release](https://github.com/softprops/action-gh-release)), an install snippet, the chart archive and the SBOM files |

The web app's build stage runs on the runner's platform (`FROM --platform=$BUILDPLATFORM`), because its output is static files; only the nginx stage is per platform. The Python images build their virtual environment per platform under QEMU (wheels are architecture specific), which makes the arm64 half the slow part of the job. Native arm64 runners (`ubuntu-24.04-arm`) would remove the emulation if they become available for the repository; the images have no architecture specific steps either way.

Install a release:

```bash
helm install mowdeck oci://ghcr.io/lukislp/charts/mowdeck --version 0.1.0 -n mowdeck --create-namespace -f my-values.yaml
helm show values oci://ghcr.io/lukislp/charts/mowdeck --version 0.1.0     # the commented values
```

## Making the packages public

GHCR packages of a private repository are private. Pulling them needs `docker login ghcr.io` (or an `imagePullSecrets` entry, see [Helm operations](helm.md#prerequisites)) with a token that has `read:packages`. To publish MowDeck without logins, change the visibility of each package once (GitHub: profile or organisation, Packages, `mowdeck/<image>` and `charts/mowdeck`, Package settings, Change visibility to public; [GHCR documentation](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)). New tags of a public package stay public.

## Rolling back

Images are immutable per version, so a rollback is choosing an older version:

```bash
helm history mowdeck -n mowdeck
helm rollback mowdeck <revision> -n mowdeck --wait          # previous chart, values and images
# or pin the image tag of a known good release with the current chart:
helm upgrade mowdeck oci://ghcr.io/lukislp/charts/mowdeck --version <current> -n mowdeck \
  --reuse-values --set image.tag=0.1.0 --wait
```

Database migrations are not rolled back: they are backward compatible, and the older images start against the newer schema (their `wait-for-schema` init container accepts a revision it does not know). A rollback across a release whose migrations were not backward compatible needs a database restore ([Backups](helm.md#backups)). The authorization model follows the release: rolling back runs the older bootstrap Job, which writes the older model as the latest one.

With Compose, the stack runs `mowdeck/<service>:${MOWDECK_VERSION}`; to run a published version instead of a local build, pull and retag it (`docker pull ghcr.io/lukislp/mowdeck/<service>:0.1.0 && docker tag ghcr.io/lukislp/mowdeck/<service>:0.1.0 mowdeck/<service>:0.1.0` for every image), set `MOWDECK_VERSION=0.1.0` and start with `docker compose up -d --no-build --wait`.

## Signing (not yet enabled)

Images and the chart are not signed yet. Keyless signing with [Cosign](https://docs.sigstore.dev/cosign/signing/signing_with_containers/) fits the workflow: add `id-token: write` to the `images` and `chart` jobs, install `sigstore/cosign-installer`, and run `cosign sign --yes ghcr.io/<owner>/mowdeck/<image>@<digest>` (digest from the build step's output) and `cosign sign --yes ghcr.io/<owner>/charts/mowdeck@<digest>` after `helm push`. Verification then pins the workflow identity: `cosign verify --certificate-identity-regexp 'https://github.com/lukislp/mowdeck/.github/workflows/release.yml@refs/tags/v.*' --certificate-oidc-issuer https://token.actions.githubusercontent.com <image>`.
