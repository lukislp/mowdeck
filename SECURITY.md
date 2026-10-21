# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting:
https://github.com/lukislp/mowdeck/security/advisories/new

Do not open a public issue for security problems. You will get an acknowledgement within a few
days and a fix or mitigation plan before any public disclosure. A report that involves a mower
manufacturer's cloud or firmware is passed on to the manufacturer only with your consent.

## Supported versions

Only the latest release and the `main` branch receive security fixes.

## Verifying releases

Releases come only from the release workflow on `main` (`.github/workflows/release.yml`). The tag `v<version>` is an SSH-signed annotated tag that GitHub shows as verified. Every container image and the Helm chart in GHCR are signed keyless with Sigstore (Cosign) and carry a GitHub build provenance attestation (SLSA provenance); every file attached to a GitHub release has a Sigstore bundle (`<file>.sigstore.json`), the release lists their checksums in `SHA256SUMS` and their build provenance in `mowdeck-<version>.intoto.jsonl`. The signing identity is `https://github.com/lukislp/mowdeck/.github/workflows/release.yml@refs/heads/main` with the issuer `https://token.actions.githubusercontent.com`:

```bash
cosign verify ghcr.io/lukislp/mowdeck/<image>:<version> \
  --certificate-identity https://github.com/lukislp/mowdeck/.github/workflows/release.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify oci://ghcr.io/lukislp/mowdeck/<image>:<version> -R lukislp/mowdeck
gh attestation verify mowdeck-<version>.tgz -R lukislp/mowdeck --bundle mowdeck-<version>.intoto.jsonl
```

All commands, including the release files and the tag: [docs/ops/release.md](docs/ops/release.md#verifying-a-release). Do not run an image or chart whose signature does not verify against this identity, and report it.

## Security baseline

- The baseline is the OWASP ASVS (Application Security Verification Standard); each phase gate reviews the relevant chapters.
- Secrets never go into the repository or into container images. Configuration and secrets are passed through environment variables or secret files at runtime. `.env` files are git-ignored; only `.env.example` with placeholder values is committed.
- Tenant isolation is the main risk of the platform: every service ships tests that request foreign-account data and expect rejection.
- Parsers of untrusted input (mower frames, bridge records, tokens and tickets, NATS and WebSocket messages, third-party API answers) are fuzzed continuously with ClusterFuzzLite, the web app's parsers have property-based tests ([docs/dev/fuzzing.md](docs/dev/fuzzing.md)). A crash found by fuzzing that has security impact is handled like any other report.
