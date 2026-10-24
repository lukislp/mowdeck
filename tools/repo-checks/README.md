# mowdeck-repo-checks

Pytest tests that guard repository conventions. They run as part of `uv run poe test`.

| Check | What it guards |
| --- | --- |
| compose | `infra/compose/docker-compose.yml` parses as YAML and `docker compose config -q` passes (skipped if Docker is missing); Traefik trusts forwarded headers only from `GATEWAY_TRUSTED_PROXIES` (empty by default) on both entry points |
| ADRs | every file in `docs/adr/` named `NNNN-*.md` has Status, Context, Decision, Consequences headings |
| packages | every directory under `services/` and `libs/` has `README.md` and `pyproject.toml` |
| docs | `mkdocs build --strict` succeeds (skipped if mkdocs is not importable) |
| images | every image default in the Dockerfiles (`ARG *_IMAGE`, `FROM`), Compose (`${*_IMAGE:-...}`, snippets) and the CI workflows (service containers, CI-only images) agrees with the `*_IMAGE` lines of `infra/compose/.env.example`; the workflows use no image mirror and the kind test pushes nothing |
| release licences | nothing a release publishes violates a licence: no workflow builds or pushes `integrations/agora-bridge` or the removed `vision-capture` (Agora SDKs, never redistributed); the released Dockerfiles name no Agora SDK (the vision Dockerfile has no capture build any more) and keep `WEB_AGORA_SDK=exclude`; no released Python image depends on an Agora package or Playwright (uv.lock) and the GPL-3.0 adapter only ends up in device-gateway; every released image declares `org.opencontainers.image.licenses` and ships the licence texts under `/usr/share/doc/mowdeck`; the web build runs its bundle guard; every mirrored third-party image has a checked, redistributable licence (`MIRROR_LICENCES`); no Agora binary is committed. `scripts/licence_gate.py` is the release workflow's image gate (job `licence-gate`: Syft SBOM, image file system, web bundle, label) |
| source offer | `scripts/source_offer.py` writes a release's `SOURCE-OFFER.md` from the SPDX SBOMs: every GPL, LGPL and AGPL component with version, images and the location of its source (Debian snapshot, Alpine aports, PyPI, upstream, this repository at the tag) after a written three-year offer; the release workflow writes it before the checksums and signatures |

Run on its own: `uv run pytest tools/repo-checks`.
