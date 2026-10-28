# Apple Home and Google Home (Matter)

Your mower can appear in the **Home** app on the iPhone (and in Google Home or Home Assistant) as its own device: start, pause, send it home, see its state and battery, use it in scenes, automations and with Siri ("Hey Siri, starte Luba"). Your server's operator turns this on once ([Matter server](../ops/matter.md)).

The Home app shows the mower as a **robot vacuum**: Matter has no robotic mower type yet. "Saugen starten" starts mowing.

## What you need

- MowDeck with the Matter server: **Einstellungen** shows a section **Matter**. If it is missing, ask your operator.
- The right to control the mower (**Mäher steuern**). You only see mowers you may control.
- An Apple home hub (Apple TV or HomePod) at home, so the mower also works away from home and in automations; Google Home needs a Nest hub.
- Your iPhone on the same home network as the MowDeck server while pairing.

## Pair a mower

1. In MowDeck open **Einstellungen › Matter**. Each mower shows **Nicht gekoppelt** or **Gekoppelt · n Controller**.
2. Tap **Kopplungscode anzeigen**. You see a QR code and an 11-digit code like `3497-011-2332`.
3. On the iPhone open **Home › + › Gerät hinzufügen** and scan the QR code (or **Weitere Optionen…** and type the code).
4. iOS warns **Nicht zertifiziertes Gerät**: MowDeck is not certified by the Connectivity Standards Alliance. Tap **Trotzdem hinzufügen**.
5. Choose a room and a name. Done: the tile shows the state and the battery.

Tap **Code ausblenden** afterwards. The code is like a key to the mower's Home tile: do not share it.

Google Home: **+ › Gerät einrichten › Neues Gerät › Matter-Gerät**, then the same code. Home Assistant: **Einstellungen › Geräte & Dienste › Matter › Gerät hinzufügen**.

## In the Home app

| Home app | What the mower does |
| --- | --- |
| Start (Saugen) | Mows every area with the mower's saved mowing settings (like Start in MowDeck); continues a paused job |
| Pause | Pauses the job |
| Zur Ladestation / Zurück zur Basis | Drives home; the job can be resumed |
| Stop | Ends the job and drives home |
| State | Mäht, Pausiert, Kehrt zurück, Lädt, An der Station, Fehler |
| Battery | The mower's battery; "low" below 20 % |

If the mower is offline or installing an update, the Home app shows an error and nothing is sent. **There is no emergency stop in the Home app**: use MowDeck, the Apple Watch, Home Assistant through MowDeck, or the stop button on the mower.

## A second app (Apple and Google)

The code only works for the first pairing. To add the mower to a second app, turn on pairing mode in the first one (Apple Home: long-press the tile › settings › **Kopplungsmodus aktivieren**), which gives you a new temporary code for the second app.

## Reset the pairing

**Einstellungen › Matter › Kopplung zurücksetzen** (confirm with **Ja, zurücksetzen**) removes the mower from every app it is paired with and creates a new code. Use it when you gave the code to someone, sold the mower, or the tile is stuck. Afterwards remove the dead tile in the Home app and pair again.

## When something does not work

- **The Home app does not find the mower:** the iPhone must be on the same Wi-Fi as the server; ask your operator whether the Matter ports are open (some routers block mDNS between Wi-Fi and LAN).
- **"Keine Antwort":** the server is down or the mower was reset; check **Einstellungen › Matter**.
- **A new mower is missing in Einstellungen › Matter:** it appears within a minute after it was added to MowDeck.
