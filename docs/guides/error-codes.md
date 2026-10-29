# Error codes in plain words

Mowers report faults as codes of their maker (a Mammotion mower says `1201`, the simulator `stuck`). MowDeck explains every code in plain words: what happened, how bad it is, the likely cause and what to do, in German or English. The words come from the error code list ("lexicon") of your mower's maker, which ships with MowDeck's connection for that maker ([ADR 0067](../adr/0067-error-code-lexicon.md)).

## Where you see them

- **The mower page** shows the current error as a card: severity and kind of problem, the maker and the code, the title, what happened, the likely cause and the steps. A lift or tilt gets a striped warning edge.
- **Health** puts the meaning next to every counted code under "Errors by code".
- **Push notifications** for a stuck mower carry the title and the first step.
- **iPhone widgets, the Live Activity and the Apple Watch** show the title ([iPhone widgets](ios-widgets.md)).

## All codes of your mower: Settings, "Error codes"

Open the mower, then **Einstellungen** (settings). The **Error codes** section lists every code the maker's mowers can report, ordered by code:

- **Search** by code or by words ("1201", "angehoben", "Ladestation"). Every word you type has to match.
- **Filter** by kind of problem (stuck, obstacle, lifted or tilted, battery, ...) and by severity (notice, warning, fault, critical). The numbers beside them count the whole list.
- **Only codes this mower reported** narrows the list to the codes in this mower's health history, plus its current error.
- Tap a row to open the full explanation.

Marks in the list:

| Mark | Meaning |
| --- | --- |
| Striped top edge, "Current error" | The mower reports this code right now; the entry is also pinned above the list. |
| "3× on this mower" | How often this mower entered this error (from its health history). |
| "Unconfirmed" | The maker has not documented this code; MowDeck's words are its best reading. |

The section only reads. It works the same for every maker MowDeck supports, also in the demo. When the list cannot be loaded (the mower has not reported yet, or the server's mower connection is restarting), the section says so; try again a little later.

## For developers

The list comes from `GET /api/v1/telemetry/mowers/{id}/error-lexicon` (search `q`, `category`, `severity`, repeatable `code`, `offset`, `limit`, `locale`); see the [API reference](../reference/api/telemetry.md). How a maker's lexicon is added: [Adding an error lexicon for a vendor](../dev/error-lexicon.md).
