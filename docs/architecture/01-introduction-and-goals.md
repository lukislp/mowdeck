# 1. Introduction and goals

## Purpose

MowDeck is an independent, multi-user control centre for robotic mowers: everything the owner does with the mower (status, map, jobs with their settings, schedules, cameras, notifications, an optional AI assistant) in one self-hosted web app. It consists of small services that scale individually, runs at home on a mini PC as well as in the cloud, and depends on no cloud provider. It is manufacturer-independent by construction: the services only know the `mowdeck-control` interface, and a manufacturer is an adapter package behind it (capabilities tell the app what a mower can do). The first adapter is for Mammotion mowers.

It is open and self-hostable without cloud lock-in, and the mower control is a reusable building block (`mowdeck-control`) that other programs can use.

## Requirements overview

- **Audience:** first the owner and household, later other households and businesses on the same installation.
- **Accounts, rights and fleet:** accounts (household or business) with sub-users, roles built from single permissions, and fleet management. Data is strictly separated per account.
- **Mower connection:** through a manufacturer adapter behind `mowdeck-control`. For Mammotion: PyMammotion over the manufacturer cloud today, Bluetooth via an ESP32 bridge on the mower as a second link.
- **Positioning:** own RTK station and Mammotion network RTK over 4G are both supported.
- **UI:** web app that installs on phones (PWA), German by default and English; optional store app later.
- **Home Assistant:** own HACS integration as a client of the public API.
- **Login:** username and password first; further methods (OAuth such as GitHub, later passkeys) as plug-ins.
- **Subscription and payment:** optional service, switchable per installation (off for the owner household).
- **Operation:** self-hosted (Docker Compose) or cloud (Kubernetes) with the same images.

## Feature scope (by phase)

| Feature | Phase |
| --- | --- |
| Accounts, sub-users, invitations, custom roles | 1 Foundation |
| Status (battery, mowing state, RTK fix, connectivity) with live updates; connection choice; map; start/pause/dock; cut height and speed; notifications | 2 MVP |
| Home Assistant, schedules, rain pause, health history, joystick, RTK signal map, theft alarm, multiple users and themes | 3 Comfort |
| Billing, plan limits, operator console, sites, groups, fleet map, bulk commands, reports, fleet API | 4 Fleet & Subscription |
| Spot mowing with patterns, re-mow missed spots, live camera | 5 Spot & Camera |
| Ai Recover, Find Dock | 6 AI |

## Quality goals

| Priority | Goal | Meaning |
| --- | --- | --- |
| 1 | Tenant isolation | No account can see or control data of another account |
| 2 | Safety | Commands expire, are deduplicated, and emergency stop has priority |
| 3 | Operability | One `docker compose up` at home; same images in Kubernetes |
| 4 | Extensibility | New features as new modules or services without changing existing ones |
| 5 | Light footprint | Runs on a Raspberry Pi 5 with 4 GB |

## Stakeholders

| Role | Expectation |
| --- | --- |
| Owner / household user | Reliable control and live status of own mowers |
| Sub-user (family, staff) | Limited rights, possibly without e-mail address |
| Operator (business, later) | Fleet overview, bulk commands, reports |
| Developer / contributor | Clear service pattern, contracts, fast checks |
| Integrator (Home Assistant) | Stable, versioned public API with limited tokens |
