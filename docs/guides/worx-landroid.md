# Worx Landroid, Kress and LandXcape mowers

MowDeck can link the app account of a Worx Landroid, Kress or LandXcape mower (the three brands share one cloud) and show and control its mowers next to your other ones. The provider is **untested on real hardware and off by default**; it was built and tested against recorded-shape data and a fake cloud only ([ADR 0066](../adr/0066-cloud-provider-plugins-and-more-vendors.md)).

!!! warning "Unofficial API, account block risk"
    MowDeck talks to the cloud the way the brand's app does. The vendor does not document or support this interface; it can change or stop working at any time. The vendor has temporarily blocked accounts that logged in or polled too often. MowDeck logs in once per account, lives on push updates and waits after a refused login, but cannot rule a block out. Use a second account the mower is shared with if the app offers one.

## Switch it on (operators)

Add `worx` to the enabled cloud providers of the Device-Gateway and restart it:

```bash
# infra/compose/.env
GATEWAY_CLOUD_PROVIDERS_ENABLE=worx
```

(Kubernetes: `GATEWAY_CLOUD_PROVIDERS_ENABLE` in the Helm values; the gateway variable is `MOWDECK_CLOUD_PROVIDERS_ENABLE`.) Several providers are comma separated, for example `husqvarna,worx`.

## Link the account

1. In MowDeck open **Settings, Linked accounts** and pick **Worx Landroid, Kress, LandXcape** in the vendor rail.
2. Pick the brand of the app your mower is registered in.
3. Enter the e-mail address and the password of that app account. The password is stored encrypted and never shown again.
4. MowDeck lists the account's mowers; add them like any other cloud mower.

No developer account is needed.

## What works

| Feature | Worx mowers in MowDeck |
| --- | --- |
| State, battery, charging, Wi-Fi signal, firmware | yes |
| Errors in plain words (English and German) | yes, all codes; the texts are marked as not confirmed by the vendor |
| Theft alarm when the mower is lifted or upside down | yes |
| Rain sensor | shown; the **rain delay** is a setting on the mower's settings page (where the mower reports one) |
| GPS position | models with the GPS module or an RTK position |
| Start | **Start now**, **Start in zone 1 to 4** (wire models with zones set up in the app) and **Cut the edge** (models with the one-time schedule), offered behind the Start button |
| Pause, resume, return to the station | yes; returning always ends the job (the mower's own schedule picks up), so MowDeck does not ask whether to keep it |
| Cutting height | models with the electronic height adjustment, 30 to 60 mm in 5 mm steps |
| Emergency stop | a pause sent at once (the cloud has no hard stop) |

Not available, so MowDeck shows no button for it: a map with areas, mowing patterns, manual driving, the camera and light of Vision models, MowDeck's own schedules (the mower follows the schedule set in its app), firmware updates.

## Mower options

On the mower's settings page:

- **Offer zones** (on by default): list the zones as start choices.
- **Command timeout** (advanced, 30 s): how long a command waits for the mower's answer.

Technical details (state table, commands, rate limits, the fake cloud for development) are in the package README, `libs/mowdeck-control-worx/README.md`.
