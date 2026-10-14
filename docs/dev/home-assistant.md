# Home Assistant integration

The Home Assistant (HACS) integration `mowdeck` lives in its own repository, [`lukislp/mowdeck-hacs`](https://github.com/lukislp/mowdeck-hacs). It was developed in this monorepo under `integrations/home-assistant` and moved out; the first entry of its `CHANGELOG.md` names the last monorepo commit. Decision record: [ADR 0025](../adr/0025-home-assistant-integration-as-api-client.md).

## Installation

HACS: add `https://github.com/lukislp/mowdeck-hacs` as a custom repository of type Integration (once that repository is public), install **MowDeck** and restart Home Assistant. Without HACS, copy `custom_components/mowdeck` into `<config>/custom_components/`. The repository README describes the API token (Settings, API tokens), the configuration, the entities (including the camera light switch and the area select with MowDeck's area names), the actions and troubleshooting.

## Contract

The integration is a pure client of the public MowDeck API and shares no code with this repository. The OpenAPI documents generated here (guarded by the contract drift tests) are therefore its contract: a change of the public API that is not backward compatible (ADR 0009) needs a matching release of the integration. Development, tests and releases of the integration are described in its own repository.
