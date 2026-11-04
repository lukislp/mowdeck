#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

# Builds every artefact MowDeck generates from its source files into one directory, for the
# reproducible build check (.github/workflows/reproducibility.yml, docs/dev/reproducible-builds.md):
#
#   <out>/python/  wheel and sdist of every uv workspace package (libs/*, services/*, tools/*,
#                  integrations/*), the packages the service images install
#   <out>/web/     the web app (apps/web/build, the SvelteKit static build the web image serves)
#   <out>/chart/   the Helm chart package (helm package, as the release workflow runs it)
#
# Usage: SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) reproducible_build.sh <out>
#
# Run it twice from clean trees of the same commit (git clean -xdf in between) and compare the
# outputs with compare_builds.py. Needs uv, pnpm (Node per .node-version) and helm on the PATH.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: SOURCE_DATE_EPOCH=<commit time> $0 <out>" >&2
  exit 2
fi
: "${SOURCE_DATE_EPOCH:?set SOURCE_DATE_EPOCH to the commit time (git log -1 --format=%ct)}"
export SOURCE_DATE_EPOCH
out="$1"
version="0.0.0-reproducibility"

rm -rf "$out"
mkdir -p "$out/python" "$out/web" "$out/chart"

echo "::group::Python packages (uv build)"
uv build --all-packages --out-dir "$out/python"
echo "::endgroup::"

echo "::group::Web app (pnpm --filter web build)"
pnpm install --frozen-lockfile --filter 'web...'
pnpm --filter web build
cp -R apps/web/build/. "$out/web/"
echo "::endgroup::"

echo "::group::Helm chart (helm package)"
helm repo add --force-update nats https://nats-io.github.io/k8s/helm/charts/
helm repo add --force-update openfga https://openfga.github.io/helm-charts
helm dependency build infra/helm/mowdeck
helm package infra/helm/mowdeck --version "$version" --app-version "$version" -d "$out/chart"
# Helm 4 writes the subcharts in map order: sort the entries (as the release workflow does).
python3 tools/repo-checks/scripts/normalize_chart.py "$out/chart/mowdeck-$version.tgz"
echo "::endgroup::"
