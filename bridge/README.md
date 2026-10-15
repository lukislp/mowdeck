# bridge

The Bluetooth bridge of MowDeck: a small ESP32 board on the mower that carries its Bluetooth radio to the Device-Gateway over Wi-Fi.

**v1 (current):** stock ESPHome with the `bluetooth_proxy` component. The YAML template, the shopping list and the flashing steps are in [docs/ops/ble-bridge-esphome.md](../docs/ops/ble-bridge-esphome.md); the template carries `esphome.project: {name: mowdeck.bridge, version: "1"}`, so MowDeck can tell which template version a bridge runs and mark older ones as outdated. A bridge is used either as the second link of a cloud mower (added from the mower's settings in the web app; manual driving takes it, everything else stays with the cloud) or as the standalone connection kind `ble_bridge` ([ADR 0038](../docs/adr/0038-dual-link-per-mower-and-firmware-updates.md), [ADR 0019](../docs/adr/0019-mammotion-adapter-transports.md)).

**v2 (planned):** a MowDeck-built firmware image rendered from a template in this repository (`templates/bridge/`), installed over the air from the web app with the stored bridge secrets (Compose profile `bridge` with an ESPHome container), so an "outdated" bridge is updated with one click. Until then the ESPHome dashboard or `esphome run` updates a bridge.
