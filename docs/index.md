# MowDeck

MowDeck is a self-hosted control centre for robotic mowers: live status and map, starting a job with your own settings, spot mowing that MowDeck steers itself, weather-aware schedules, the cameras, notifications and an optional AI assistant, for one household or several accounts, with the data kept at home. It is made of small, separately scalable services and runs on a mini PC at home (Docker Compose) as well as on Kubernetes with the same images. It is not tied to one manufacturer: the services use the `mowdeck-control` interface, and each manufacturer is an adapter package behind it. The first adapter is for Mammotion (LUBA, YUKA), built on PyMammotion; a simulator implements the same interface.

**Status:** Phases 1 to 4 are done (foundation, web app, scheduler and integrations, fleet and entitlements); the camera, spot-mowing and AI features of Phases 5 and 6 are in use. See [Phases and gates](phases.md).

## Where to go next

- [Phases and gates](phases.md): roadmap and the Phase 1 acceptance criterion
- [Architecture (arc42)](architecture/01-introduction-and-goals.md): goals, context, building blocks, runtime, deployment
- [Architecture decisions](adr/README.md): ADRs
- [Getting started](dev/getting-started.md) and the [service template](dev/service-template.md)
- [Web app](dev/web-app.md): developer guide of the PWA and the design system
- [Operating the Compose stack](ops/compose.md)
- [Spikes](spikes/index.md): research documents
- [Original project plan (German)](plan/project-plan.de.md), the authoritative source this documentation condenses
