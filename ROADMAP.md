# Roadmap

What the project intends to work on over the next twelve months (until the end of 2027). It is a statement of direction from the maintainer ([GOVERNANCE.md](GOVERNANCE.md)), not a promise with dates. Priorities change, and the reasons go into the linked issues, pull requests or ADRs. Requests and comments are welcome in [Discussions](https://github.com/lukislp/mowdeck/discussions).

The detailed, item-by-item list of what is done and what is still open lives in [docs/phases.md](docs/phases.md), section "Milestones after Phase 6". This page groups the larger directions.

## Mowers and vendors

- **Provider-neutral core, one adapter package per vendor.** Mammotion (LUBA 2 AWD X, the reference mower), Husqvarna Automower Connect and Worx Landroid are integrated. Each vendor stays a plugin with a capability profile and the provider conformance kit ([ADR 0066](docs/adr/0066-cloud-provider-plugins-and-more-vendors.md), [ADR 0069](docs/adr/0069-provider-conformance-kit-and-package-template.md), [ADR 0071](docs/adr/0071-capability-matrix.md)). New vendors come in only through that path.
- **Hardware confirmation of the open items** that need the real LUBA 2 AWD X: stereo camera streams, the headlamp, the firmware update tracker, several mowers on one cloud session, the Bluetooth bridge after the `habluetooth` 7 update ([docs/phases.md](docs/phases.md#milestones-after-phase-6), [vendor library updates](docs/ops/vendor-lib-updates.md)).
- **Virtual zones**: zone events and notifications, coverage per zone ([ADR 0070](docs/adr/0070-virtual-zones.md)).
- **AI garden mapping** (survey run): design first, as an ADR, before any code.

## Apps and integrations

- The native shells (iOS, Android, Apple Watch, widgets, Live Activity) through TestFlight and the Android release ([ADR 0036](docs/adr/0036-native-app-shells-with-capacitor.md), [mobile apps](docs/ops/mobile-apps.md)).
- The Home Assistant integration ([mowdeck-hacs](https://github.com/lukislp/mowdeck-hacs)) follows the typed MowDeck fields. Approving vision proposals stays in MowDeck itself.
- Matter pairing with real home hubs ([ADR 0063](docs/adr/0063-matter-server.md)).

## Security and supply chain

- **Revocation that ends access tokens in every service at once** (today a revoked session ends at the next refresh, at most 10 minutes later) ([ADR 0065](docs/adr/0065-security-center-and-new-login-alerts.md)).
- **The camera relay's RTSP feed over the loopback.** Every other hop inside the stack is encrypted ([internal TLS](docs/ops/internal-tls.md)); MediaMTX moves into camera-relay's network namespace so the RTSP feed between them never crosses the network either.
- **NetworkPolicies in the Helm chart** ([security assurance case](docs/dev/security-assurance.md#residual-risks-accepted)).
- **Per-service database roles in Compose** instead of one shared service password ([risks and technical debt](docs/architecture/11-risks-and-technical-debt.md)).
- **Dynamic application scanning** (an OWASP ZAP baseline against the e2e stack) on top of the existing fuzzing.
- **SPDX licence headers** in source files, checked in CI. That needs care because of the three licences ([LICENSING.md](LICENSING.md)).
- **Reproducible builds**: document and check how far two builds of the same commit produce identical images.
- Keep the OpenSSF Scorecard at its current level or higher, and reach the OpenSSF Best Practices passing badge.

## Project continuity

- Keep the continuity mitigations in [GOVERNANCE.md](GOVERNANCE.md#continuity-what-happens-if-the-maintainer-is-unavailable) true: a public build anyone can repeat, keyless signing, and no secret that only the maintainer holds that a fork would need for the server images.
- Labelled starter issues (`good first issue`) so new contributors have a way in.
- A public documentation site built from `docs/` (today: MkDocs locally and the GitHub wiki mirror).

## Maintenance (continuous)

- Dependency updates through Dependabot. No known vulnerability of medium or higher severity stays unfixed for more than 60 days ([SECURITY.md](SECURITY.md)). Runtime upgrades (Node LTS, Python) follow their support windows.
- The test suites stay green and coverage does not drop (numbers in the CI job summary, [CI](docs/dev/ci.md)).
- The documentation stays in line with the code: the OpenAPI documents, the API reference, the capability matrix and the architecture diagrams are generated and checked in CI.
