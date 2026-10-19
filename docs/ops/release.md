# Releases

Releases are cut by [semantic-release](https://semantic-release.gitbook.io/) from the commits on `main`: there is no manual tagging. Every push to `main` runs `.github/workflows/release.yml`, which asks semantic-release (configuration in `.releaserc.json`, toolchain from the shared `lukislp/ci-workflows` actions `semantic-release` and `deploy-key-push`) whether the commits since the last tag warrant a release. If they do, the same run creates the signed tag and the GitHub release, builds and publishes the images, the Helm chart and the Android app from that tag, and signs everything it attaches. The decisions are in [ADR 0029](../adr/0029-helm-chart-and-release-pipeline.md).

## How a merge becomes a release

1. A pull request is merged into `main`. Its commit messages are [Conventional Commits](https://www.conventionalcommits.org/) and decide the release (table below): a squash merge leaves one commit with the pull request title (or the single commit's message), a merge or rebase keeps every commit, and the highest release type among them wins.
2. Job `version`: semantic-release dry run from the tip of `main`. Docs, ci, chore, test and style commits produce no release; the run ends there, green.
3. Job `tag`: semantic-release creates the annotated tag `v<version>`, signed with the SSH key `DEPLOY_SIGNING_KEY` (GitHub shows it as "Verified"), pushes it, and creates the GitHub release with notes generated from the commits, grouped into Features and Bug Fixes, with links to the pull requests. It comments "released in" on the merged pull requests and closed issues.
4. Jobs `licence-gate`, `images`, `chart`, `android` build the tagged commit, which is always the commit of the run itself: a run whose commit is no longer the tip of `main` releases nothing, and the run of the newer push releases instead (table under [What the workflow publishes](#what-the-workflow-publishes)). Each image index and the chart are signed keyless with Sigstore and get a GitHub build provenance attestation in the registry.
5. Job `publish` attaches the chart archive, the SBOMs and the APK with `SHA256SUMS`, a Sigstore bundle per file and the build provenance file, and appends the install snippet, the digest of every image and the chart, and the verification commands to the release notes.

The tag push starts `helm-kind.yml` (the kind install test) when the deploy key is set; a tag pushed with `GITHUB_TOKEN` starts no workflow. The release version is set at build time (image labels and tags, chart `version` and `appVersion`, APK), so no file in the repository carries it and a release makes no commit; `infra/helm/mowdeck/Chart.yaml` keeps `0.1.0-dev`, `pyproject.toml` keeps `0.0.0`. The changelog is the list of GitHub releases; there is no `CHANGELOG.md`.

## Commit types and versions

| Commit | Release while the version is 0.x | Example |
| --- | --- | --- |
| `fix:`, `perf:`, `refactor:` | patch (0.4.1 to 0.4.2) | `fix(fleet): keep the last position when RTK drops` |
| `feat:` | minor (0.4.2 to 0.5.0) | `feat(planner): spot mowing around a point` |
| `feat!:`, `fix!:` or a `BREAKING CHANGE:` footer | minor (0.5.0 to 0.6.0), never 1.0.0 | `feat(api)!: drop the v0 mower endpoints` |
| `docs:`, `ci:`, `chore:`, `test:`, `style:`, `build:` | none | `docs(ops): explain the backup restore` |

**The 0.x line.** MowDeck stays below 1.0.0 until the owner decides otherwise. On 0.x a breaking change is allowed in a minor version ([Semantic Versioning, item 4](https://semver.org/#spec-item-4)), so `.releaserc.json` maps every breaking change to a minor release (`"releaseRules": [{"breaking": true, "release": "minor"}, ...]`); the `!` after the type is recognised through the commit analyzer's `parserOpts`. `refactor` releases a patch, as in the other repositories that use the shared actions. The release job refuses to publish when no `v*` tag exists, because semantic-release would then start at 1.0.0: the line starts from the initial tag `v0.0.0` (see [Setting up releases](#setting-up-releases)).

**Going to 1.0.0 deliberately.** In one pull request, remove the rule `{"breaking": true, "release": "minor"}` from `.releaserc.json` and give the merged commit a `BREAKING CHANGE:` footer (for example `feat!: MowDeck 1.0` with the footer `BREAKING CHANGE: first stable release, see the upgrade notes`). semantic-release then publishes 1.0.0, and from then on a breaking change is a major release as usual. Update this page and the "Releases" section of `CONTRIBUTING.md` in the same pull request.

## Setting up releases

One-time setup by the owner; the release job fails with a pointer to this section while a piece is missing.

1. **Signing key** (repository secret `DEPLOY_SIGNING_KEY`): a dedicated ed25519 SSH key whose public half is a signing key of the owner's account (github.com/settings/keys, "Signing key"); the release job checks this before it tags. The tagger is the owner's noreply address, which GitHub needs to show the tag as verified. Also set it as a Dependabot secret if Dependabot pull requests should ever run the release (they do not today).
2. **Deploy key** (repository secret `SEMANTIC_RELEASE_DEPLOY_KEY`, optional but recommended): a write deploy key of this repository. semantic-release then pushes the tag over SSH as the key, which starts the tag workflows (`helm-kind.yml`). The tag ruleset blocks deleting, moving and force-pushing tags, not creating them, so the key needs no ruleset bypass as long as releases make no commit.
3. **Initial tag** `v0.0.0` on the last commit of `main` before release automation, created while `release.yml` is disabled, because the workflow file of older commits still publishes on every tag push:

   ```bash
   gh workflow disable release.yml -R lukislp/mowdeck
   gh workflow disable helm-kind.yml -R lukislp/mowdeck
   git fetch origin && git tag -s v0.0.0 -m "MowDeck 0.0.0 (start of the 0.x line)" origin/main
   git push origin v0.0.0
   gh workflow enable release.yml -R lukislp/mowdeck
   gh workflow enable helm-kind.yml -R lukislp/mowdeck
   ```

   `v0.0.0` is never published. The first releasable merge after it becomes `v0.1.0` (a `feat`) or `v0.0.1` (a `fix`).

**Before the first release:** if packages with these names (`mowdeck/<image>`, `charts/mowdeck`) already exist under the owner on GHCR, grant this repository write access to them or remove them before the first release; otherwise the push is refused.

**A failed release.** When a job after `tag` fails, the tag and the GitHub release already exist; re-run the failed jobs of the same workflow run (`gh run rerun <run-id> --failed`), which builds from the same tag and fills in the assets. Do not delete or move a published tag.

**Test builds without a release:** `gh workflow run release.yml -f version=0.1.0-rc.1` (optionally `--ref <branch>`) builds, signs and pushes the images and the chart under that version, without a tag, a GitHub release or `:latest`.

## What the workflow publishes

| Job | Output |
| --- | --- |
| `version` | the next version from the semantic-release dry run (or the input of a test build), checked against semantic versioning; the checks for the initial tag and the signing key |
| `tag` | the SSH-signed annotated tag `v<version>` and the GitHub release with the generated notes (`@semantic-release/github`) |
| `licence-gate` (one job per image, before anything is pushed) | builds each image for linux/amd64 into the runner only and runs `tools/repo-checks/scripts/licence_gate.py` on it: a Syft SBOM with licences, the image's file system, the web bundle and the `org.opencontainers.image.licenses` label. It fails on any Agora package or file (the Agora SDKs are proprietary and are never redistributed), a licence that is not open source, a label that differs from the expected one or missing licence texts; `images` only starts after it passed. See [Licences](#licences). |
| `images` (one job per image, in parallel) | `identity`, `fleet`, `device-gateway`, `telemetry`, `realtime`, `notifications`, `scheduler`, `entitlements`, `billing`, `planner`, `vision`, `camera-relay`, `web` and `authz-bootstrap` for **linux/amd64 and linux/arm64** as one multi-platform image each: `ghcr.io/<owner>/mowdeck/<image>:<version>`, `:v<version>` and, for a release, `:latest`. Built with Buildx, QEMU for arm64 ([docker/setup-qemu-action](https://github.com/docker/setup-qemu-action), [multi-platform builds](https://docs.docker.com/build/building/multi-platform/)); base images as pinned in `infra/compose/.env.example`. Each image index carries an SBOM and a provenance attestation ([build attestations](https://docs.docker.com/build/metadata/attestations/sbom/)), a [Cosign](https://docs.sigstore.dev/cosign/signing/signing_with_containers/) keyless signature and a [GitHub build provenance attestation](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations/using-artifact-attestations-to-establish-provenance-for-builds), and gets an SPDX SBOM file from [anchore/sbom-action](https://github.com/anchore/sbom-action). |
| `chart` | `helm package` with `version` and `appVersion` set to the release version (so `image.tag` defaults to the released images), pushed as an OCI artifact to `oci://ghcr.io/<owner>/charts/mowdeck:<version>` ([Helm registries](https://helm.sh/docs/topics/registries/)), signed and attested like the images |
| `android` | `mowdeck-<version>.apk`, signed with the release keystore ([mobile apps](mobile-apps.md)); skipped with a warning while the keystore secret is not set |
| `publish` | the release assets `mowdeck-<version>.tgz`, `sbom-<image>.spdx.json`, `mowdeck-<version>.apk`, `SOURCE-OFFER.md` (see [Source code offer](#source-code-offer)), `SHA256SUMS`, `<file>.sigstore.json` for each of them and `mowdeck-<version>.intoto.jsonl`, plus the install snippet, the digests and the verification commands in the notes |

The web app's build stage runs on the runner's platform (`FROM --platform=$BUILDPLATFORM`), because its output is static files; only the nginx stage is per platform. The Python images build their virtual environment per platform under QEMU (wheels are architecture specific), which makes the arm64 half the slow part of the job. Native arm64 runners (`ubuntu-24.04-arm`) would remove the emulation if they become available for the repository; the images have no architecture specific steps either way.

Install a release:

```bash
helm install mowdeck oci://ghcr.io/lukislp/charts/mowdeck --version 0.1.0 -n mowdeck --create-namespace -f my-values.yaml
helm show values oci://ghcr.io/lukislp/charts/mowdeck --version 0.1.0     # the commented values
```

## No caches in release jobs

The release jobs build without any shared cache: no Buildx `type=gha` cache, no npm/pnpm or Gradle cache. A cache entry is written by whichever workflow run gets there first (a pull request, a test build by hand), so restoring one into a release build could put content into a published image that no reviewed commit produced (cache poisoning; CodeQL's `actions/cache-poisoning`). The price is a slower release run, mostly the arm64 half of the Python images under QEMU.

## Source code offer

The images contain GPL, LGPL and AGPL software: MowDeck itself (AGPL-3.0, the GPL-3.0 adapter and its dependency PyMammotion in `device-gateway`), the GNU and other copyleft packages of the Debian and Alpine base images, Debian's ffmpeg with x264 in `camera-relay`, and busybox in `authz-bootstrap`. Every release therefore carries `SOURCE-OFFER.md`, written by `tools/repo-checks/scripts/source_offer.py` in the `publish` job from the SPDX SBOMs of the release: each GPL-family component with its version, the images that contain it and the location of its corresponding source (the Debian source package in the snapshot archive, the Alpine aports branch and distfiles, the PyPI source distribution, upstream for busybox, this repository at the tag for MowDeck's own packages), after a written offer to provide the source on request for at least three years (requests through an issue titled `Source request v<version>` or the repository's private reporting channel). It is listed in `SHA256SUMS`, signed and covered by the build provenance like every other asset. A component the script cannot place is listed with "on request"; answer such a request from the image itself (`docker run --rm --entrypoint sh <image>` and the distribution's source tools) within the offer period.

## Making the packages public

GHCR packages start private. Pulling them needs `docker login ghcr.io` (or an `imagePullSecrets` entry, see [Helm operations](helm.md#prerequisites)) with a token that has `read:packages`. To publish MowDeck without logins, change the visibility of each package once (GitHub: profile or organisation, Packages, `mowdeck/<image>` and `charts/mowdeck`, Package settings, Change visibility to public; [GHCR documentation](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)). New tags of a public package stay public.

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

## Licences

What a release publishes and under which licence ([LICENSING.md](https://github.com/lukislp/mowdeck/blob/main/LICENSING.md)):

| Artifact | `org.opencontainers.image.licenses` | Notes |
| --- | --- | --- |
| `identity`, `fleet`, `telemetry`, `realtime`, `notifications`, `scheduler`, `entitlements`, `billing`, `planner`, `vision`, `camera-relay` | `AGPL-3.0-only AND Apache-2.0` | the service (AGPL-3.0) and the `libs/*` it uses (Apache-2.0); `vision` without the capture worker (`WITH_CAPTURE=false`), `camera-relay` with Debian's ffmpeg (GPL, Debian main) |
| `device-gateway` | `AGPL-3.0-only AND Apache-2.0 AND GPL-3.0-only` | contains the GPL-3.0 adapter `mowdeck-control-mammotion` and PyMammotion (GPL-3.0); AGPL-3.0 section 13 and GPL-3.0 section 13 allow the combination |
| `web` | `AGPL-3.0-only AND Apache-2.0` | built with `WEB_AGORA_SDK=exclude`: no Agora Web SDK; `/THIRD-PARTY-LICENSES.txt` lists every npm package in the bundle with its licence text |
| `authz-bootstrap` | `AGPL-3.0-only AND Apache-2.0 AND MIT AND GPL-2.0-only` | bootstrap script (AGPL-3.0), the OpenFGA CLI (Apache-2.0), jq (MIT), busybox (GPL-2.0) |
| Helm chart | AGPL-3.0 | templates only; no agora-bridge image (the operator builds it, the chart refuses to render the bridge without one) |

Every service and web image carries `LICENSE`, `LICENSING.md` and `LICENSES/` under `/usr/share/doc/mowdeck`; the third-party Python packages keep their licence files in their `dist-info`, the Debian and Alpine packages under `/usr/share/doc` and `/usr/share/licenses`. The SPDX SBOM files attached to the release list every package with its declared licence.

Never published: `agora-bridge` (contains Agora's native SDK), `vision-capture` (contains the Agora Web SDK) and a web image built with `WEB_AGORA_SDK=include`. Operators who want them build them for their own installation ([integrations/agora-bridge](https://github.com/lukislp/mowdeck/blob/main/integrations/agora-bridge/README.md), [apps/web](https://github.com/lukislp/mowdeck/blob/main/apps/web/README.md#agora-web-sdk-and-third-party-licences)). `tools/repo-checks` (`test_release_licences.py`) keeps them out of every workflow, and the `licence-gate` job checks the built images again.

## Verifying a release

Every signature is bound to the workflow identity `https://github.com/lukislp/mowdeck/.github/workflows/release.yml@refs/heads/main` and the issuer `https://token.actions.githubusercontent.com`; a signature made by any other workflow, branch or repository fails the check. The tools: [Cosign](https://docs.sigstore.dev/cosign/system_config/installation/) and the [GitHub CLI](https://cli.github.com/) (`gh attestation`). While the packages are private, `docker login ghcr.io` first (see [Making the packages public](#making-the-packages-public)).

```bash
VERSION=0.1.0
ID=https://github.com/lukislp/mowdeck/.github/workflows/release.yml@refs/heads/main
ISSUER=https://token.actions.githubusercontent.com

# The tag: signed annotated tag, verified by GitHub
gh api repos/lukislp/mowdeck/git/tags/$(gh api repos/lukislp/mowdeck/git/ref/tags/v$VERSION --jq .object.sha) --jq .verification

# An image (by tag or, better, by the digest from the release notes)
cosign verify ghcr.io/lukislp/mowdeck/identity:$VERSION --certificate-identity "$ID" --certificate-oidc-issuer "$ISSUER"
gh attestation verify oci://ghcr.io/lukislp/mowdeck/identity:$VERSION -R lukislp/mowdeck

# The chart in the registry
cosign verify ghcr.io/lukislp/charts/mowdeck:$VERSION --certificate-identity "$ID" --certificate-oidc-issuer "$ISSUER"

# The release assets
gh release download v$VERSION -R lukislp/mowdeck -D mowdeck-$VERSION && cd mowdeck-$VERSION
sha256sum -c SHA256SUMS
cosign verify-blob --bundle SHA256SUMS.sigstore.json --certificate-identity "$ID" --certificate-oidc-issuer "$ISSUER" SHA256SUMS
cosign verify-blob --bundle mowdeck-$VERSION.tgz.sigstore.json --certificate-identity "$ID" --certificate-oidc-issuer "$ISSUER" mowdeck-$VERSION.tgz
gh attestation verify mowdeck-$VERSION.tgz -R lukislp/mowdeck --bundle mowdeck-$VERSION.intoto.jsonl
```

`gh attestation verify` without `--bundle` fetches the attestation from GitHub; the attached `mowdeck-<version>.intoto.jsonl` (a Sigstore bundle with the in-toto SLSA provenance statement over every asset of the release) lets the check run offline. A test build by hand is signed with the identity of the branch it ran on (`...release.yml@refs/heads/<branch>`), so it never passes as a release.
