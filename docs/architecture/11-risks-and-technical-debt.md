# 11. Risks and technical debt

| Risk | Mitigation |
| --- | --- |
| Unofficial protocol: a Mammotion firmware update can break features | Pin the PyMammotion version; apply mower firmware updates only after community feedback |
| Double access while the existing Mammotion integration in Home Assistant runs | In the spike use a second shared Mammotion account; remove the old integration after the MVP |
| Safety when driving or spot mowing | Dead-man switch, blade off in transfer, visible emergency stop, never bypass sensor stops |
| Login and access | Pluggable login, Argon2id, API tokens per client, login rate limit, short sessions, access from outside only via Tailscale or HTTPS proxy |
| Maintenance effort of many small programs | Monorepo, shared contracts, one CI, versioned public API early |
| Terms of use | Uncritical for personal use; review before redistribution |
| WLAN control not yet available | Bluetooth is default; WLAN is a second adapter behind the same interface |
| Microservice complexity for one household | Same pattern for every service, one Compose file, interfaces that would allow several services in one process |
| Data leaks between accounts | Account ID only from token, filter in each query, per-service tests with foreign data |
| Distributed failures | Command IDs, expiry, emergency stop priority, traces across services |
| Payment and legal duties once others pay | Only in Phase 4; choose a provider that handles taxes |

## Open questions for the spike

- Does PyMammotion deliver map geometry (zones, paths, dock) in a usable form?
- Does it support manual driving and mowing jobs with own limits?
- How does the camera stream leave the mower and can it be tapped locally?
- What is the command latency over the Bluetooth bridge, and is it enough for the joystick?
- Which entities and services should the HACS integration offer?
- Does it fully support 4G mowers and network RTK, including fix status and cellular connection?
- Does the mower report the correction source (station or network RTK)?
- Which bridge firmware: ESPHome Bluetooth proxy or a lean custom ESP32 firmware?
- Payment provider for EU customers: Stripe with tax module or a merchant of record such as Paddle?
- How many mower connections can one Device-Gateway instance hold (sharding size)?
- Can the LUBA drive arbitrary paths or only zones with its own patterns (own route versus native job)?

## Technical debt

None recorded yet. Known deliberate shortcuts: the Compose stack uses one shared password for all service database roles and mounts the Docker socket read-only into Traefik; both are acceptable for the development defaults and must be revisited before cloud operation.
