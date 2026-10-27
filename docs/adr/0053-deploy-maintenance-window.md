# ADR 0053: Deploy maintenance window

## Status

Accepted (2026-10-27)

## Context

Every deploy of the live stack restarts the Device-Gateway. For a second or two the mower is seen offline, and Notifications sent `mower_offline` and `mower_online` to every device (seen 2026-10-24 and 2026-10-27). The owner declined a general offline grace period: a real outage must still be reported at once. So the deploy has to tell Notifications that the next link loss is its own doing.

## Decision

**A maintenance window in Notifications' database.** One row (table `maintenance_window`, `id = 1`, migration 0007): `opened_at`, `expires_at`, `closed_at`, `reason`. It expires by itself (the deploy asks for 15 minutes; the CLI accepts at most `MOWDECK_NOTIFICATIONS_MAINTENANCE_MAX_MINUTES`, default 60), so a forgotten window cannot silence alerts for long. There is no HTTP endpoint and no authentication surface: the window is written by a command inside the notifications container, which already holds the database credentials.

**CLI.** `python -m notifications.cli maintenance open --minutes 15 --reason "deploy 0.9.0"`, `maintenance close`, `maintenance status`. The deploy runs them with `docker compose exec -T notifications ...`. The output is one line and never contains a secret.

**Suppression of the pair, not of the kinds.** Only `mower_offline` and `mower_online` are affected; they are the only link-state kinds. Theft alarms (`mower_lifted`, `mower_left_dock_unexpectedly`, `geofence_exit`), errors of the mower (`mower_stuck`) and all other kinds are never held. A mower that goes offline is held back (the watch is offline with `offline_held`, nothing is sent) when the offline began between `opened_at - lead` (default 60 s: the last state arrived shortly before the restart) and the end of the window (the close, at the latest the expiry), and it is noticed no later than the end plus a grace (default 120 s). Because no offline went out, no online follows. If the mower comes back, the hold is simply cleared. The offline timer also sweeps held watches: when the window and its grace are over and the mower is still offline, `mower_offline` is sent then, so a mower that really died during the deploy is reported two minutes after the close at the latest. An offline that began before the window (a real earlier outage) or that is noticed after the grace is sent as always.

**Deploy script.** `infra/compose/deploy.sh <version>` runs the documented procedure (fetch tags, backup, checkout, `verify-release.sh`, set `MOWDECK_VERSION`, pull, `up -d --no-build --wait`) with the window opened after the pull and closed after `up --wait`, also when `up` fails. Opening is best effort: the first deploy of a build that has the window still runs the old notifications image without the command, so the script warns and goes on (that one deploy may still send the pair).

## Consequences

Deploys are silent for the owner; real outages that coincide with a deploy are reported at most about two minutes after the close, otherwise immediately. The window is global to the installation (one stack), not per account. Replicas read the window with a two second cache. Helm: the chart has no hook yet; a pre/post-upgrade Job running the same CLI is possible and left for later. A banner for operators in the web app is not part of this decision.
