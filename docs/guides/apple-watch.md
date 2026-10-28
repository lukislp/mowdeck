# Apple Watch

The MowDeck app on the Apple Watch shows your mower's state and sends **Start**, **Pause**, **Station** and the **emergency stop** (Not-Stopp). A complication puts battery and state on the watch face. It needs the MowDeck iPhone app from TestFlight (a build with the watch app) and watchOS 10. Your server's operator sets up the build once ([Apple Watch](../ops/mobile-apps.md#apple-watch)).

## How it works

The watch has no login of its own and never talks to the MowDeck server. Every command goes to your iPhone, and the iPhone sends it with the access of its widgets ([iPhone widgets](ios-widgets.md)). So:

- **Widgets must be on** in the iPhone app (**Einstellungen**, panel **This app**, **Turn on widgets**). You do not have to put a widget on the home screen.
- **The iPhone must be near the watch** (Bluetooth or the same Wi-Fi) and must reach your server: at home, or with your VPN on the iPhone. The watch says **iPhone nicht erreichbar** when it cannot reach the iPhone, and **Server nicht erreichbar, letzter bekannter Stand** when the iPhone could not reach the server.
- A lost watch carries no key: there is nothing on it to revoke. Turning widgets off on the iPhone, or revoking "iOS widgets: ..." under **Einstellungen -> API-Tokens**, stops the watch at once.

## Install

1. Install or update MowDeck from TestFlight on the iPhone.
2. Open the **Watch** app on the iPhone, scroll to **Available Apps**, and install **MowDeck** (or turn on automatic app installation).
3. In the MowDeck iPhone app, turn on widgets if they are not on. The settings panel shows **Apple Watch: bereit** when everything fits.
4. Open MowDeck on the watch. It shows your mower (or a list when you have several).

## On the watch

| You see | Meaning |
| --- | --- |
| State and glyph (Mäht, Pausiert, Fährt zur Station, Station, Lädt, Fehler, Regenpause, Offline) | The same words as the iPhone widgets |
| Battery ring | Green, orange at 30 % and below, red at 15 % and below; a bolt while charging |
| Progress bar | While a job runs, with the area and the minutes left |
| Red text | The mower's error |
| "Stand vor ..." | The values are older than 15 minutes: the iPhone has not sent anything new |

The buttons depend on the state: **Start** at the station (all areas with the mower's saved mowing defaults, like Start in the app), **Pause** while mowing, **Station** while mowing, paused, idle or in an error. Under the buttons the watch says what happened: "Start gesendet", or why not (for example "Du darfst diesen Mäher nicht steuern"). "Gesendet" means the server accepted the command; the next state shows what the mower did.

## Emergency stop

The red **Not-Stopp** button is always there. Tap it, confirm with **Not-Stopp** (or **Abbrechen**), and the watch sends it at once: blades and wheels stop. If the iPhone does not answer, the watch tries again for about two seconds and then tells you it could not send it. It never asks for your password or passkey: stopping the mower must always be quick, and anyone who may see the mower may stop it.

If the watch says it could not send the stop, use the app, Home Assistant, or the stop button on the mower.

## Complication

Long-press the watch face, **Edit**, pick a slot, and choose **MowDeck**. The complication shows the mower you opened last in the watch app: battery ring and state glyph (round), state and battery (inline, corner), or name, state, battery and progress (rectangular). It greys out when its values are older than 15 minutes. watchOS limits how often an app may refresh its complication (about 50 times a day): MowDeck spends that only when the face would change visibly, so a tiny battery change can wait until you open the app.

## Good to know

- Commands are never queued: if the iPhone cannot be reached, nothing is sent later.
- If you closed the iPhone app by swiping it away, iOS may not wake it for the watch until you open it once.
- Several mowers: the watch shows up to eight.
