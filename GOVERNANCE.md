# Governance

MowDeck is a **single-maintainer project**. This file says who decides what, how decisions are made, and what happens when the maintainer is unavailable. It describes how the project works today, not a larger team.

## Roles

| Role | Who | Responsibilities |
| --- | --- | --- |
| **Maintainer** | [@lukislp](https://github.com/lukislp) (Lukas Körber), the only person with write and admin access | Sets the direction and the [roadmap](ROADMAP.md). Reviews and merges pull requests. Triages issues, discussions and security reports ([SECURITY.md](SECURITY.md)). Tests every update of a mower vendor library on a real mower before it is merged ([vendor library updates](docs/ops/vendor-lib-updates.md)). Enforces the [Code of Conduct](CODE_OF_CONDUCT.md). Administers the repository, its secrets, the `ghcr.io/lukislp/mowdeck` packages and the public demo. |
| **Contributor** | Anyone who opens an issue, a discussion or a pull request | Follows [CONTRIBUTING.md](CONTRIBUTING.md) and the Code of Conduct. Contributors need no write access: every change arrives as a pull request from a fork. |
| **Automation** (no human authority) | Dependabot, `dependabot-auto-merge.yml`, semantic-release, the release workflow | Dependabot proposes dependency updates as pull requests. The auto-merge workflow approves patch and minor updates and merges them only once every required check is green. It never touches majors or the mower vendor libraries (`pymammotion`, `pyworxcloud`, `aioautomower`), which stay a manual decision after a hardware test. semantic-release tags releases and writes the release notes from Conventional Commits, and the release workflow builds and signs what it publishes. None of them can bypass the required checks on `main`. |

There are no other roles today: no co-maintainers, no committee, no release managers. A contributor with sustained, high-quality contributions can be offered co-maintainer access (see [Becoming a maintainer](#becoming-a-maintainer)), and this file is updated when that happens.

## How decisions are made

- **Day-to-day changes** (bug fixes, dependency updates, small features) are decided in the pull request: the required checks must be green, and the maintainer merges.
- **Bigger changes** (a new feature, a new external dependency, an API, contract or data-model change, anything that affects self-hosters, the Home Assistant integration or the apps) start as an issue or a [discussion](https://github.com/lukislp/mowdeck/discussions), so the direction is agreed before the work starts. Anyone can comment. The maintainer makes the final call and states the reason there.
- **Architecture decisions** are recorded as ADRs in [`docs/adr/`](docs/adr/README.md), with context, the decision, the alternatives that were rejected and the consequences. A change that reverses or extends a decision updates its ADR or adds a new one in the same pull request ([definition of done](CONTRIBUTING.md#definition-of-done)).
- **Security decisions** follow [SECURITY.md](SECURITY.md) and the [security assurance case](docs/dev/security-assurance.md), which lists the accepted residual risks explicitly.
- **Mower safety** (emergency stop, manual driving, commands that move a mower) follows [ADR 0012](docs/adr/0012-mower-control-interface-and-safety-rules.md). A change that weakens one of its rules needs a new ADR, not just a pull request.
- **Breaking changes** are marked in the pull request title (`feat!:`, `fix!:` or a `BREAKING CHANGE:` footer). The release notes then list them under "BREAKING CHANGES" ([releases](docs/ops/release.md#commit-types-and-versions)).

## Releases

Releases are automated. Every push to `main` runs semantic-release, and the Conventional Commit types since the last tag decide whether and how the version moves (0.x line). The release workflow publishes signed container images, the signed Helm chart and the release files with build provenance, and an SSH-signed tag ([SECURITY.md](SECURITY.md#verifying-releases), [releases](docs/ops/release.md)). Only the latest release is supported.

## Continuity: what happens if the maintainer is unavailable

The bus factor is **1**, and this file does not pretend otherwise. No second person has admin access to the repository, the `ghcr.io/lukislp/mowdeck` namespace, the demo host or the release secrets. What exists instead:

- **The code and the history are public.** Anyone can fork the repository (AGPL-3.0, libraries Apache-2.0 or GPL-3.0, see [LICENSING.md](LICENSING.md)). A fork keeps the full history, the issues stay readable, and every published release stays downloadable from GitHub Releases and `ghcr.io`.
- **Nothing needed to build lives only with the maintainer.** The build is `uv` + `pnpm` + Docker from this repository ([getting started](docs/dev/getting-started.md), [CONTRIBUTING.md](CONTRIBUTING.md#running-checks-locally)). Container images, the Helm chart and the release files are signed keyless with Sigstore, bound to the GitHub Actions workflow identity, so no private key is needed to sign them, and a fork gets its own identity the same way. The CI workflows run unchanged in a fork. Only the steps that need a secret of their own need the fork's own keys: the release tag's SSH signing and deploy key, the Android keystore, Apple signing, and the auto-merge token. An app signed by a fork is therefore a different app to the stores and to Android.
- **Installations keep running.** Each installation is self-hosted and independent of the maintainer: no licence server, no phone-home, no central account service. Updates stop, but nothing breaks at a deadline. Mower vendor clouds can still change their APIs, and that is outside the project's control in any case.
- **No successor arrangement exists.** There is no GitHub account successor, no second owner and no organisation account, and none is planned at the moment. Nobody else can take over the repository, the package namespace or the demo. Continuity rests entirely on the points above: public code, a build anyone can repeat, and keyless signing that a fork can set up for its own identity.
- **If the maintainer stops responding** (no reaction to issues, pull requests or security reports for more than 30 days, with no announcement), treat the project as unmaintained: fork it, rebuild from source, and keep the AGPL obligations ([source code for network users](LICENSING.md#source-code-for-network-users-agpl-30-section-13)). If a community fork emerges, the maintainer, once reachable again, will link it from the README.

## Becoming a maintainer

There is no formal application. A contributor who has had several non-trivial pull requests merged, has reviewed others' changes and has shown good judgement on security, mower safety and compatibility can be invited by the maintainer. A new maintainer gets write access first, and admin access only after a while. The new role, and any change to the decision process, is recorded in this file by pull request.

## Code of Conduct

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md). The maintainer enforces it.

## Changes to this document

This file changes like any other: by pull request, merged by the maintainer.
