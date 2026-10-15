/**
 * Online or offline from the gateway's link heartbeat (ADR 0040), and the age of the values as a
 * separate fact.
 *
 * `heartbeat_at` is when the Device-Gateway last knew its connection to the mower alive; it
 * advances with every heartbeat (10 s) even when the mower reports nothing new, as a docked mower
 * does. `observed_at` is when the mower reported the values. A mower is online while its last
 * heartbeat is younger than {@link OFFLINE_AFTER_MS} and the gateway has not reported the link
 * lost (`connected: false`). States from before the field carry no `heartbeat_at`; then
 * `observed_at` stands in, as it did before.
 */
import type { components } from '$lib/generated/telemetry';

type MowerState = components['schemas']['MowerState'];
/** The fields presence reads (spot conditions pass a narrower state). */
interface LinkFields {
  observed_at: MowerState['observed_at'];
  heartbeat_at?: string | null;
  connected?: boolean | null;
}

/**
 * A mower whose last link heartbeat is older than this counts as offline. The gateway sends one
 * every 10 s, so 45 s means four missed heartbeats.
 */
export const OFFLINE_AFTER_MS = 45_000;

/** The values of an online mower are pointed out as old once they are older than this. */
export const VALUES_OLD_AFTER_MS = 3 * 60_000;

/** When the link was last known alive, in epoch milliseconds (`observed_at` as fallback). */
export function linkSeenAt(state: LinkFields): number {
  const observed = Date.parse(state.observed_at);
  const beat = state.heartbeat_at ? Date.parse(state.heartbeat_at) : Number.NaN;
  return Number.isNaN(beat) ? observed : Math.max(beat, observed);
}

/** True while the gateway holds a live connection to the mower. */
export function isOnline(state: LinkFields, now: number): boolean {
  if (state.connected === false) return false;
  return now - linkSeenAt(state) <= OFFLINE_AFTER_MS;
}

/**
 * The age of the values in milliseconds when the mower is online and they are older than
 * {@link VALUES_OLD_AFTER_MS}; null otherwise (fresh values, or offline, where the last contact
 * is what counts).
 */
export function oldValuesAge(state: MowerState, now: number): number | null {
  if (!isOnline(state, now)) return null;
  const age = now - Date.parse(state.observed_at);
  return age > VALUES_OLD_AFTER_MS ? age : null;
}

/**
 * Whether `next` may replace `current`: a newer report always, the same report with a link
 * heartbeat that is not older (heartbeats and the offline state repeat `observed_at`).
 */
export function isNewer(current: MowerState, next: MowerState): boolean {
  const a = Date.parse(current.observed_at);
  const b = Date.parse(next.observed_at);
  if (a !== b) return b > a;
  return linkSeenAt(next) >= linkSeenAt(current);
}
