# Chat control

Tell a mower what to do in your own words. On the mower page, the box **Tell the mower** ("Sag's dem Mäher") takes a request such as:

- "mäh morgen früh nur hinten" / "mow the back lawn tomorrow morning": mow once, tomorrow at 08:00, only the area named like "back";
- "jetzt vorne mähen" / "mow the front now": start now on the front area;
- "schick ihn zur Station" / "send it to the station": back to the charging station;
- "pausier" / "pause", "weitermachen" / "resume".

MowDeck answers with a **work order**: the exact actions, the mower, the areas and the time. **Nothing happens until you press "Confirm and run".** Then the actions go out exactly like the buttons of the mower page (a later start becomes a one-off schedule you find under Schedules). An order expires after five minutes; ask again then.

The box appears only when your installation runs the optional assistant ([operations](../ops/assistant.md)).

## What it cannot do

- **Drive the mower by hand.** That needs the live view and the drive controls.
- **Change settings** (blade height, speed, patterns, recurring schedules). Use the settings pages.
- **Resume at a time** ("pause until 3 pm"): the pause works, resume it yourself later.
- Offer what you may not do: a user who may only view a mower gets no actions, and starts need map areas.

## The AI provider

Chat control reads your requests with the AI provider under **Settings, AI provider** (your own, or your installation's model). Without one the box links there. Your request goes to that provider together with the mower's name, its area names and the actions you may use; nothing else. The provider can misread a request: always check the work order before you confirm it.

An installation may run chat control with a built-in phrase parser instead (always in demo mode): then no AI is involved, the box says "Phrase parser, no AI", and it understands the examples above.
