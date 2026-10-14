# 10. Quality requirements

| Quality | Scenario | Measure |
| --- | --- | --- |
| Security / isolation | A user of a foreign account requests a mower, a schedule or telemetry of another account | Rejected in every service; covered by automated tests per service |
| Security / commands | A stale or duplicated start command arrives | Dropped because of expiry or duplicate command ID |
| Safety | Emergency stop is sent while other commands are queued | Stop is handled with priority |
| Safety | Joystick connection drops | Driving command expires after about 1 second |
| Availability | One service fails or restarts | Other services keep working; commands are replayed from JetStream where valid |
| Operability | Fresh install at home | `docker compose up` starts the full stack |
| Portability | Same images at home and in the cloud | Only configuration differs; images for amd64 and arm64 |
| Efficiency | Raspberry Pi 5 with 4 GB | Foundation stack fits in memory |
| Extensibility | New permission, plan feature or UI area | Added by a self-describing module without changing existing services |
| Maintainability | Any service | Same layers and a README; lint, types and tests enforced in CI |
| Compatibility | API or event change | Backward compatible only, or a new version |
| Security baseline | Login, tokens, secrets | Reviewed against OWASP ASVS; Argon2id; rate limit on login; secrets never in images |
