# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting:
https://github.com/lukislp/mowdeck/security/advisories/new

If you cannot use GitHub, email the maintainer at koerber.lukas@t-online.de with "SECURITY" in the subject line instead.

Do not open a public issue, discussion or pull request for a security problem. If you are not sure whether something is a vulnerability, report it privately anyway.

A useful report has the affected version (Settings, About, or the `X-Source-Code` header of any response), the deployment (Compose, Helm, the public demo, an app), steps to reproduce and the impact you expect. A report that involves a mower manufacturer's cloud or firmware is passed on to the manufacturer only with your consent.

## What happens after you report

MowDeck has one maintainer ([GOVERNANCE.md](GOVERNANCE.md)), so these are targets one person can actually meet:

| Step | Target |
| --- | --- |
| Acknowledgement of the report | within **3 days** |
| First assessment (accepted, needs more information, or not a vulnerability, with a CVSS severity) | within **14 days** |
| Fix released for **critical** and **high** severity | within **30 days** of acceptance |
| Fix released for **medium** and **low** severity | within **60 days**, or with the next regular release |

The process:

1. **Triage.** The maintainer reproduces the issue in the private advisory, agrees on a severity with the reporter and decides whether it is in scope (below).
2. **Fix.** The fix is developed in the advisory's private fork or on a branch that does not reveal the vulnerability. It comes with a regression test ([CONTRIBUTING.md](CONTRIBUTING.md#tests)) and ships through the normal signed release pipeline. A fix that touches mower safety (emergency stop, driving) is also tested on a real mower.
3. **CVE and advisory.** For an accepted vulnerability in MowDeck itself, the maintainer requests a CVE through GitHub (GitHub is a CVE Numbering Authority). The GitHub Security Advisory is published once the fixed release is out. It names the fixed version, and the release notes link to it.
4. **Disclosure.** The default is coordinated disclosure: the details go public with the advisory, once the fix is released or after 90 days, whichever comes first. A different date can be agreed with the reporter.
5. **Credit.** Reporters are credited by name or handle in the advisory, unless they ask not to be.

If a target slips, the reporter is told why and gets a new date. If you get no acknowledgement within 7 days, comment on the advisory again or send an email.

## Scope

**In scope:** this repository: the services, the web app, the native app shells, the libraries under `libs/` (including the GPL adapter packages), the bridge configuration, the Compose stack and the Helm chart, the CI and release workflows, and the public demo at https://mowdeck-demo.lktec.org.

**Out of scope here:**

- vulnerabilities in a mower manufacturer's cloud, app or firmware, or in the vendor libraries (`pymammotion`, `pyworxcloud`, `aioautomower`). Report those to the manufacturer or the library's project, and tell us too if MowDeck makes them worse or exposes them;
- the Home Assistant integration ([mowdeck-hacs](https://github.com/lukislp/mowdeck-hacs)), which has its own repository, unless the flaw is on MowDeck's side of the API;
- publicly known vulnerabilities in third-party dependencies, which Dependabot already tracks;
- findings that need an already-compromised host, container or cluster node, or an installation that departs from the documented setup (for example, the development values of `infra/compose/.env.example` in production).

Please do not run load tests or automated scanners against the public demo.

## Supported versions

Only the latest release and the `main` branch receive security fixes. There are no maintenance branches. The way to get a fix is to upgrade: each service's database migrations run on start (the `*-migrate` containers in Compose), and they are backward compatible ([rolling back](docs/ops/release.md#rolling-back)).

## Verifying releases

Releases come only from the release workflow on `main` (`.github/workflows/release.yml`). The tag `v<version>` is created by the lktec-bot GitHub App through the GitHub API and carries no signature of its own; what to verify are the signed images, chart and release files below. Every container image and the Helm chart in GHCR are signed keyless with Sigstore (Cosign) and carry a GitHub build provenance attestation (SLSA provenance). Every file attached to a GitHub release has a Sigstore bundle (`<file>.sigstore.json`), and the release lists their checksums in `SHA256SUMS` and their build provenance in `mowdeck-<version>.intoto.jsonl`. The signing identity is `https://github.com/lukislp/mowdeck/.github/workflows/release.yml@refs/heads/main` with the issuer `https://token.actions.githubusercontent.com`:

```bash
cosign verify ghcr.io/lukislp/mowdeck/<image>:<version> \
  --certificate-identity https://github.com/lukislp/mowdeck/.github/workflows/release.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify oci://ghcr.io/lukislp/mowdeck/<image>:<version> -R lukislp/mowdeck
gh attestation verify mowdeck-<version>.tgz -R lukislp/mowdeck --bundle mowdeck-<version>.intoto.jsonl
```

All commands, including the release files and the tag: [docs/ops/release.md](docs/ops/release.md#verifying-a-release). For a Compose installation, `infra/compose/verify-release.sh` runs these checks and pins every image by its verified digest ([Compose](docs/ops/compose.md)). Do not run an image or chart whose signature does not verify against this identity, and report it.

## Security baseline

- The baseline is the OWASP ASVS (Application Security Verification Standard), and each phase gate reviews the relevant chapters. How the requirements are met, the trust boundaries and the accepted residual risks are in the [security assurance case](docs/dev/security-assurance.md).
- Secrets never go into the repository or into container images. Configuration and secrets are passed through environment variables or secret files at runtime. `.env` files are git-ignored, and only `.env.example` with marked development values is committed. Identity and Fleet refuse their development encryption keys with `MOWDECK_ENVIRONMENT=prod`.
- Tenant isolation is the main risk of the platform: every service ships tests that request foreign-account data and expect rejection.
- Parsers of untrusted input (mower frames, bridge records, tokens and tickets, NATS and WebSocket messages, third-party API answers) are fuzzed continuously with ClusterFuzzLite, and the web app's parsers have property-based tests ([docs/dev/fuzzing.md](docs/dev/fuzzing.md)). A crash found by fuzzing that has security impact is handled like any other report.
