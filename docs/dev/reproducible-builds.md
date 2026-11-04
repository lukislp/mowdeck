# Reproducible builds

What MowDeck builds from its source files, how two builds of the same commit come out bit for bit identical, how that is checked, and where the limits are.

## What is covered

| Artefact | Built by | Released as |
| --- | --- | --- |
| Wheel and sdist of every uv workspace package (`libs/*`, `services/*`, `tools/*`, `integrations/*`) | `uv build --all-packages` (hatchling) | installed into the service images |
| Web app (`apps/web/build`: the SvelteKit static build, `csp.conf`, licence notices, the API explorer) | `pnpm --filter web build` | served by the `web` image |
| Helm chart package (with the NATS and OpenFGA subcharts) | `helm package infra/helm/mowdeck` | `mowdeck-<version>.tgz` on the release, `oci://ghcr.io/lukislp/charts/mowdeck` |
| Every image the release publishes (13 Python services, `matter-server`, `web`, `authz-bootstrap`) | `docker buildx build` with `SOURCE_DATE_EPOCH` and `rewrite-timestamp=true` | `ghcr.io/lukislp/mowdeck/<image>` |

The inputs are pinned: `uv.lock` and `pnpm-lock.yaml` (installed frozen), the chart's `Chart.lock`, base images by digest (the Dockerfiles and `infra/compose/.env.example`), the tool versions in the workflows (uv, Node from `.node-version`, pnpm from `packageManager`, Helm) and the GitHub Actions by commit SHA. `SOURCE_DATE_EPOCH` is the commit time (`git log -1 --format=%ct`); the release workflow and the check set it.

## Sources of non-determinism and their fixes

| Where | What differed | Fix |
| --- | --- | --- |
| Web app | SvelteKit's app version defaults to the build time; it ends up in `_app/version.json`, `200.html`, the service worker and the CSP hash of the inline script | `version.name` in `apps/web/vite.config.ts` is `SOURCE_DATE_EPOCH` when set (the build time otherwise, as before) |
| Helm chart | file times in the package were the packaging time | `helm package` with `SOURCE_DATE_EPOCH` (Helm 4 uses it for every entry) |
| Python images | `.pyc` files recorded the time the sources were unpacked; uv's parallel bytecode workers produced different `.pyc` bytes for the same source from run to run; `uv_cache.json` of the workspace packages recorded the install time | the bytecode is compiled after `uv sync` in one process in a fixed order with `PYTHONHASHSEED=0`, hash-based (`checked-hash`) because `SOURCE_DATE_EPOCH` is set; `uv_cache.json` (uv's bookkeeping, not read at runtime) is deleted together with its `RECORD` line |
| `camera-relay` image | fontconfig's cache, ldconfig's `aux-cache` and `alternatives.log` of the Debian package installation carried the build time | `SOURCE_DATE_EPOCH` during the installation, the log and `aux-cache` removed |
| `matter-server` image | `node_modules/.modules.yaml` (pnpm's install state) carried the install time | removed; Node never reads it |
| All images | file times in the layers, image creation time | `SOURCE_DATE_EPOCH` and BuildKit's `rewrite-timestamp=true` (release workflow and check) |

The Python wheels and sdists needed no change: hatchling writes fixed timestamps and sorted archives.

## Checking it

`.github/workflows/reproducibility.yml` runs weekly, on demand (`gh workflow run reproducibility.yml`) and on pull requests that change build inputs (Dockerfiles, lock files, `pyproject.toml` files, the web build configuration, the chart metadata, the scripts below). It is not a required check.

- **artefacts**: `tools/repo-checks/scripts/reproducible_build.sh` builds the Python packages, the web app and the chart into one directory; then `git clean -xdf` and the same again; `tools/repo-checks/scripts/compare_builds.py` compares the two directories file by file with SHA-256.
- **images** (one job per released image, `linux/amd64`): two builds without cache as OCI layouts with the release's build arguments, `SOURCE_DATE_EPOCH` and `rewrite-timestamp=true`, compared the same way. Identical layouts mean identical layer, config and manifest digests.

There is no list of accepted exceptions: any difference fails the run, and the job summary lists the files that differ.

Measured on 2026-11-04 (commit `587e07f0` plus this change, two clean builds each): artefacts **340/340 files** identical (52 wheels and sdists of 26 packages plus uv's `.gitignore`, 286 web files, the chart), images **16/16** identical (`linux/amd64`, every OCI blob).

### By hand

On Linux (or in a Linux container) with uv, pnpm (Node per `.node-version`) and Helm 4:

```sh
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
bash tools/repo-checks/scripts/reproducible_build.sh /tmp/build-a
git clean -xdf
bash tools/repo-checks/scripts/reproducible_build.sh /tmp/build-b
python3 tools/repo-checks/scripts/compare_builds.py /tmp/build-a /tmp/build-b
```

An image (BuildKit with the `docker-container` driver, which `docker buildx create --use` sets up):

```sh
for run in a b; do
  docker buildx build --no-cache --platform linux/amd64 \
    --build-arg SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
    --output "type=oci,tar=false,rewrite-timestamp=true,dest=/tmp/fleet-$run" \
    -f services/fleet/Dockerfile .
done
python3 tools/repo-checks/scripts/compare_builds.py /tmp/fleet-a /tmp/fleet-b
```

### Against a release

- **Chart**: check out the release tag, run the chart part of the release workflow (`helm dependency build`, then `helm package infra/helm/mowdeck --version <version> --app-version <version>` with `SOURCE_DATE_EPOCH` of the tag's commit) and compare the `.tgz` with the release asset (verified first as in [Releases](../ops/release.md#verifying-a-release)).
- **Image**: build from the tag with the build arguments and labels of the release workflow's `images` job (`MOWDECK_VERSION`, `MOWDECK_SOURCE_URL`, `MOWDECK_SOURCE_REF=v<version>`, the `org.opencontainers.image.*` labels) and compare the digest of the platform manifest with the one in the published index (`docker buildx imagetools inspect --raw ghcr.io/lukislp/mowdeck/<image>:<version>`). The `web` image additionally contains `whats-new.json`, which the release writes from the tags before the build (`apps/web/build-tools/whats-new.mjs`).

## Limits

- **Same checkout path for the web app.** Built from two different directories, the web app differs: Svelte's scoped CSS class names depend on the component paths, and the chunk hashes follow. The Python packages and the chart do not depend on the path. The image build always uses `/repo`, and the check builds both times in the same workspace.
- **Linux.** On Windows, the build context and the archives carry different file modes; build on Linux or in a container.
- **Debian packages of `camera-relay`.** `apt-get` installs what the Debian mirror serves on the day of the build. Two builds on the same day are identical (the check), a rebuild of an old release months later can get newer package versions. Pinning through `snapshot.debian.org` would close this.
- **Build back end.** The workspace packages require `hatchling>=1.32.4`; uv resolves the newest matching version at build time, so a much later rebuild may use a newer hatchling.
- **Image index and signatures.** The released index also carries the SBOM and SLSA provenance attestations, which record the build's own time and invocation, and the Sigstore signatures are new on every run. Compare the platform manifests, not the index digest. `linux/arm64` is built from the same Dockerfiles under QEMU and is not part of the check.
- **Hash-based bytecode.** With `SOURCE_DATE_EPOCH` set, Python checks each imported module's `.pyc` against a hash of its source instead of its time stamp: a small extra cost at start-up.
- **Not covered.** The Android APK (`apps/mobile`, Gradle, signed with the release keystore) and the iOS build (TestFlight) are not checked. `SHA256SUMS`, `SOURCE-OFFER.md`, the SPDX SBOMs and the provenance files of a release are derived from the artefacts above or from the signing run, not from the source alone.
