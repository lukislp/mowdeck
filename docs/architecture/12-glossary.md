# 12. Glossary

| Term | Meaning |
| --- | --- |
| Account | Household or business; data, rights and billing boundary |
| Sub-user | User created by an account, possibly without e-mail address |
| Role | Named bundle of single permissions |
| Permission | Single right such as `mower.control` |
| Site | Location of an account with address, time zone and own map |
| Group | Collection of mowers, possibly across sites |
| Principal | The authenticated user or API token making a request |
| Gateway | Single entry point (Traefik) |
| Device-Gateway | Service that holds connections to mowers and bridges |
| Bridge | ESP32 on the mower that connects Bluetooth with WLAN |
| PyMammotion | Python library speaking the Mammotion protocol |
| RTK | Real-time kinematic positioning for centimeter accuracy |
| Network RTK | Correction data delivered over 4G instead of an own station |
| OpenFGA | Open source relationship-based authorization service (Zanzibar model) |
| JetStream | Persistence layer of NATS |
| JWT / JWKS | Signed token / published set of public keys to verify it |
| Entitlement | Statement what an account may do under its plan |
| Mowing pattern | Plug-in that turns an area and settings into a route |
| Spot mowing | Mowing a user-drawn area of arbitrary shape |
| ADR | Architecture decision record |
| arc42 | Template for architecture documentation |
| C4 | Context, container, component, code: diagram levels |
| PWA | Progressive web app, installable from the browser |
| HACS | Home Assistant Community Store |
| Gate | Release criterion at the end of a phase |
