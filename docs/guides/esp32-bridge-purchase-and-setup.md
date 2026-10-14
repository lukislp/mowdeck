# ESP32 bridge: purchase and setup

This guide gets you from nothing to a working BLE bridge on the LUBA 2 AWD X, using the ESPHome Bluetooth Proxy as the quickest test path. It feeds steps 4 to 6 of [spike 0001](../spikes/0001-ble-bridge-and-pymammotion.md). Prices were checked on 2026-10-08 where a link is given; everything else is an approximate street price.

## What you are building

A small ESP32 board rides on the mower, keeps a Bluetooth LE connection to it at close range and talks to your home Wi-Fi. A Python process on your laptop (later the MowDeck Device-Gateway) uses the ESP32 as a remote Bluetooth radio. PyMammotion already supports this path through `bleak-esphome` ([dev_console.py](https://github.com/mikey0000/PyMammotion/blob/main/examples/dev_console.py)).

## Before you order: three checks

1. **USB port on the mower.** The LUBA 2 AWD manual lists on the rear view "SIM card tray" and "USB port for troubleshooting and debugging" ([manual PDF](https://robotlawnmowers.com.au/wp-content/uploads/2024/02/Luba_2_AWD_Series_User_Manual_V1.0_EN.pdf)). **Unverified for your X model:** connector type, whether it supplies 5 V, how much current, and whether it stays powered while the mower sleeps or charges. Look at the port (next to the SIM tray, per the manual), note the connector type and whether it has a sealing cap, and plan to measure it with a USB power meter before relying on it. One community project powers its ESP32-S3 from "the mower's USB-C port" ([mammotion-rc](https://github.com/zitafilms/mammotion-rc)), but does not name the model.
2. **Antenna.** The mower body and the board's position matter more than the chip. Pick a board with a real external antenna (U.FL) or a proven compact module. Avoid metal enclosures.
3. **Wi-Fi in the garden.** Walk the mowing area with your phone and a Wi-Fi analyser app. If the far end shows worse than about -75 dBm, plan an outdoor access point or mesh node first. A bridge without Wi-Fi is useless.

## Shopping list

| # | Item | Recommended | Alternative | Approx. price | Where |
|---|---|---|---|---|---|
| 1 | ESP32 board | Seeed Studio XIAO ESP32-S3 (BLE 5, U.FL antenna connector, USB-C) | M5Stack AtomS3 Lite (ESP32-S3 in a small plastic case, internal antenna) | 9.90 EUR each | [Berrybase XIAO ESP32-S3](https://www.berrybase.de/detail/019de2c9775872f985e5ef385c70decc?search=xiao%20esp32s3), [Berrybase AtomS3 Lite](https://www.berrybase.de/m5stack-atoms3-lite-esp32s3-dev-kit), also reichelt, Amazon.de |
| 2 | Antenna | 2.4 GHz rod or FPC antenna with U.FL plug (the XIAO ESP32-S3 is normally sold with one; check the listing says so) | U.FL to RP-SMA pigtail plus a small 2.4 GHz RP-SMA antenna through the enclosure wall | 0 to 10 EUR | same shops, Amazon.de |
| 3 | USB cable | USB-C to USB-C (or USB-A to USB-C, depending on the mower port), 20 to 30 cm, angled plug | Any short data-capable cable | 5 to 8 EUR | Amazon.de, reichelt |
| 4 | USB power meter | Inline USB-C meter showing V and mA | USB-A meter plus adapter | 10 to 15 EUR | Amazon.de |
| 5 | Enclosure | Small ABS or polycarbonate box, IP65, about 80 x 82 x 55 mm | Any IP65 plastic junction box that fits board and cable bend | 5 to 10 EUR | reichelt, Conrad, RS ([example RND 455](https://de.rs-online.com/web/p/universalgehause/2832894)) |
| 6 | Cable gland | M12 nylon gland, IP68, for 3 to 6.5 mm cable | Grommet plus sealant | 1 to 3 EUR | reichelt, RS, Amazon.de |
| 7 | Mounting | Outdoor double-sided tape (3M VHB type), UV-stable cable ties, a silica gel pack | Velcro straps | 5 to 10 EUR | hardware store, Amazon.de |
| 8 | Fallback power | 5,000 mAh power bank with an always-on or low-current mode | USB battery pack from a cycling light | 15 to 25 EUR | Amazon.de |

Buy two boards: one for the mower, one to keep on the desk for firmware work. Expected total: about 40 to 60 EUR. Mouser and Digi-Key also stock XIAO boards but only make sense when combined with a larger order because of shipping.

Why the XIAO ESP32-S3: tiny, cheap, USB-C, ESP-IDF support in ESPHome, and an external antenna you can route to the best spot. The AtomS3 Lite is the easier choice if you prefer a ready case and accept the internal antenna. Do not buy an ESP32-S2: it has no Bluetooth.

## First flash with ESPHome

You need Python with `uv` (see the spike's environment setup). Install the ESPHome CLI:

```bash
uv tool install "esphome==2026.9.1"
```

Create a folder `mowdeck-bridge/` with two files.

`secrets.yaml` (never commit it):

```yaml
wifi_ssid: "YourGardenSSID"
wifi_password: "..."
api_key: "..."          # generate: python -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())"
ota_password: "..."
fallback_password: "..."
```

`mowdeck-bridge.yaml`:

```yaml
esphome:
  name: mowdeck-bridge
  friendly_name: MowDeck Bridge

esp32:
  board: seeed_xiao_esp32s3      # AtomS3 Lite: m5stack-atoms3
  framework:
    type: esp-idf                # required for the proxy, uses less memory

logger:

api:
  encryption:
    key: !secret api_key

ota:
  - platform: esphome
    password: !secret ota_password

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
  ap:
    ssid: "MowDeck Bridge Fallback"
    password: !secret fallback_password

captive_portal:

esp32_ble_tracker:
  scan_parameters:
    active: true

bluetooth_proxy:
  active: true                   # proxy GATT connections, not only adverts
  connection_slots: 3

sensor:
  - platform: wifi_signal
    name: "WiFi signal"
    update_interval: 15s
  - platform: uptime
    name: "Uptime"

button:
  - platform: restart
    name: "Restart"
```

Option notes, from the [ESPHome Bluetooth Proxy docs](https://esphome.io/components/bluetooth_proxy/): `active: true` enables GATT connections, `connection_slots` defaults to 3 on ESP32 (maximum 9, keep it at 5 or less), and ESP-IDF is recommended. The API password option is deprecated; use the encryption key ([ESPHome API docs](https://esphome.io/components/api/)).

Do **not** add a `ble_client:` entry for the mower, even though the [Mammotion-HA ESP32 guide](https://github.com/mikey0000/Mammotion-HA/wiki/ESP32-guide) shows one. It makes the ESP itself hold the mower's connection, which probably blocks the proxied connection we need (unverified).

Flash over USB:

```bash
cd mowdeck-bridge
esphome run mowdeck-bridge.yaml          # choose the USB serial port
```

If the port does not show up or flashing fails, hold the BOOT button while plugging in the board, then try again. Later updates go over the air with the same command (choose the network target). Watch the logs:

```bash
esphome logs mowdeck-bridge.yaml --device mowdeck-bridge.local
```

You should see Wi-Fi connected, the API server listening, and BLE advertisements once the mower is on.

**Home Assistant:** HA will discover the new ESPHome device. Click "Ignore" for now. If HA adopts it, the old Mammotion integration may grab the mower through it and collide with the spike.

## Test the proxy from your laptop

1. Quick API check (in the spike project folder, with `ESPHOME_NOISE_PSK` set to `api_key`):

   ```python
   import asyncio, os
   from aioesphomeapi import APIClient


   async def main() -> None:
       api = APIClient("mowdeck-bridge.local", 6053, None, noise_psk=os.environ["ESPHOME_NOISE_PSK"])
       await api.connect(login=True)
       info = await api.device_info()
       print(
           info.name, info.esphome_version, info.bluetooth_proxy_feature_flags_compat(api.api_version)
       )
       await api.disconnect()


   asyncio.run(main())
   ```

   Expected: name, ESPHome version and a non-zero feature flag value. If `.local` does not resolve on Windows, use the IP from your router.

2. Mower over the proxy: put the ESP32 next to the mower on a USB charger, close the phone app, then run spike step 2 with `--via proxy --name <Luba name> --address <MAC>`. Battery and mode must appear. Note: the scripts use `bluetooth-adapters` to build a Bluetooth manager; if that fails on your laptop, run the same command on a Linux machine (unverified on Windows).

3. Latency: spike step 5 with `--via proxy`, first next to the mower, then mounted.

## Provisional mounting on the mower

- **Placement:** on the top rear area, as high as possible, away from metal parts. Keep clear of the camera, ultrasonic sensors, bumper, lights and the rain sensor. Do not drill or open the mower; that risks the warranty and the IPX6 seal.
- **Enclosure:** board inside the IP65 box, antenna inside or through the wall, cable through the M12 gland, silica gel pack inside. Fix the box with outdoor tape plus a cable tie as backup.
- **Power, option A (mower USB):** only after the meter shows stable 5 V with the mower switched on. Record whether power stays on while docked and charging, and while the mower sleeps. The port is a service port: if it has a cap, the cap cannot close with a cable plugged in, so shield it from rain with a drip loop and do not leave this setup out in rain until you know the port is sealed or the cable plug is protected. Unverified whether this is safe for the mower long term.
- **Power, option B (power bank):** for the first outdoor tests, a power bank in the enclosure keeps the mower's port closed. Many power banks switch off at the low current an ESP32 draws (typically around 50 to 150 mA with Wi-Fi and BLE active, a rough estimate); pick one with an always-on mode and test it overnight on the desk.
- **Check coverage:** after mounting, let the mower drive its normal route and watch the `WiFi signal` sensor in `esphome logs`. Note where it drops below about -75 dBm.

## What the custom MowDeck firmware will replace

The ESPHome proxy is the Phase 1 tool. A later MowDeck firmware (only if the spike shows it is needed) changes:

| ESPHome proxy today | MowDeck firmware later |
|---|---|
| Inbound TCP API on port 6053, client must reach the ESP on the LAN | Outbound WebSocket over TLS to the Device-Gateway, works behind any router |
| Shared Noise key per device, typed in by hand | Per-device secret, provisioned during pairing in the MowDeck app |
| Generic Bluetooth proxy for any device and Home Assistant | Single-purpose GATT tunnel for the mower's `ff01` and `ff02` characteristics |
| ESPHome OTA from your laptop | Signed OTA delivered by the Device-Gateway |
| No mower-specific safety | Local deadman: sends a stop to the mower if the gateway link drops |

An intermediate option is ESPHome with its `wireguard` component, which builds an outbound tunnel to one peer and keeps the stock proxy ([ESPHome WireGuard](https://esphome.io/components/wireguard/)). The spike results decide between the two.
