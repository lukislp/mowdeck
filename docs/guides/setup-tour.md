# The setup tour

A new MowDeck account starts with a short, guided setup. It shows what is still missing for a MowDeck that is ready to mow and leads you into the right place for each step. Every step notices by itself when it is done; nothing needs to be ticked off by hand.

## Where you find it

- **On the mower list.** A new account without a mower sees the *Einrichtung* card at the top: the progress, the next step and its button. *Tour öffnen* shows all steps, *Später* hides the card.
- **Under Einstellungen → Einrichtung.** The status of the tour and a button to open or continue it. Opening it there brings back a hidden tour with everything you did so far.
- **Accounts that were set up before the tour existed** see one line on the mower list instead ("Einrichtungshilfe: …"). *Prüfen* opens the tour, the cross hides the line for good.

The progress is stored with your user, not in the browser: what you skipped in the browser stays skipped in the iPhone app.

## The steps

| Step | Done when | Leads to |
| --- | --- | --- |
| Mammotion-Konto verbinden | a Mammotion account is linked (or you already have a mower of another kind) | Einstellungen → Verbundene Konten |
| Mäher hinzufügen | you have a mower | Mäher → Mäher hinzufügen |
| Karte prüfen | MowDeck has read the map of your mower (a few minutes after it connects) | the mower's page |
| Benachrichtigungen einrichten | push in this or another browser, the iPhone app or ntfy is set up | Einstellungen → Benachrichtigungen |
| Passkey anlegen (optional) | you have a passkey | Einstellungen → Konto |
| Ersten Mähplan anlegen | a schedule exists | Zeitpläne → Neuer Zeitplan |
| Anmeldung verknüpfen (optional) | GitHub or Apple is linked for sign-in | Einstellungen → Konto |
| Widgets und Apple Watch (optional, iPhone app only) | the widgets are on on this iPhone | Einstellungen → Benachrichtigungen (this app) |

Steps that do not apply are left out: without the right to manage mowers you do not see the first two, the passkey and sign-in steps appear only when the server offers them, and the widget step only inside the iPhone app.

**Überspringen** puts a step aside; it no longer holds up the tour, and **Wieder aufnehmen** brings it back at any time. The tour is complete when every step is done or skipped; optional steps may stay open. **Fertig** closes it; Einstellungen → Einrichtung keeps it reachable.

On an iPhone in Safari, the notification step points to the existing install hint: push notifications arrive only in the installed app.

## First-time hints

While you are new, the mower page and the schedules show a short *Zum ersten Mal hier* note at the top. **Verstanden** hides it on every device. Accounts that were set up before the tour existed never see these notes.

## For operators

The tour needs no configuration. Identity stores each user's choices in the table `onboarding_progress` (`GET`/`PUT /api/v1/identity/me/onboarding`); the decision record is [ADR 0062](../adr/0062-setup-tour.md).
