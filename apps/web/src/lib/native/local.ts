/**
 * Local notifications of the app shells, fed by the notification inbox.
 *
 * While the app runs (foreground, or background until the OS suspends it) the inbox store polls
 * the Notifications API; every new unread item becomes a local notification
 * (`@capacitor/local-notifications`). The first load only sets the baseline (nothing old pops
 * up when the app starts), and the ids already shown are remembered, so an item never notifies
 * twice. Items the user already read are skipped.
 */
import type { InboxItem } from '$lib/stores/inbox.svelte';

const SEEN_KEY = 'mowdeck.native.seen';
const SEEN_MAX = 300;

/** Android notification channels: alarms (critical) and everything else. */
export const CHANNELS = [
  { id: 'mowdeck-alarms', name: 'MowDeck alarms', importance: 5, visibility: 1, vibration: true },
  { id: 'mowdeck', name: 'MowDeck', importance: 3, visibility: 1, vibration: true },
] as const;

/** `LocalNotificationSchema` of `@capacitor/local-notifications` (the fields used here). */
export interface LocalNotification {
  id: number;
  title: string;
  body: string;
  channelId: string;
  group: string;
  extra: { url: string | null; notification_id: string; kind: string; mower_id: string | null };
}

type Store = Pick<Storage, 'getItem' | 'setItem'>;

/** A 31-bit notification id from an inbox item id (a UUID); Android ids are Java ints. */
export function notificationNumber(id: string): number {
  const hex = id.replace(/[^0-9a-f]/gi, '').slice(0, 7);
  const parsed = Number.parseInt(hex || '0', 16);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function toLocalNotification(item: InboxItem): LocalNotification {
  return {
    id: notificationNumber(item.id),
    title: item.title,
    body: item.body,
    channelId: item.severity === 'critical' ? 'mowdeck-alarms' : 'mowdeck',
    group: item.mower_id ? `mower:${item.mower_id}` : 'mowdeck',
    extra: { url: item.url, notification_id: item.id, kind: item.kind, mower_id: item.mower_id },
  };
}

export class InboxNotifier {
  readonly #storage: Store | null;
  #seen: string[];
  #baseline: boolean;

  constructor(storage: Store | null) {
    this.#storage = storage;
    const stored = this.#load();
    this.#seen = stored ?? [];
    // Without a stored list (first start of the app) the first inbox only sets the baseline.
    this.#baseline = stored === null;
  }

  /** The items of this inbox page to notify now (newest last), and remember them. */
  fresh(items: readonly InboxItem[]): InboxItem[] {
    const seen = new Set(this.#seen);
    const unseen = items.filter((item) => !seen.has(item.id));
    if (!unseen.length && !this.#baseline) return [];
    const show = this.#baseline ? [] : unseen.filter((item) => !item.read);
    this.#baseline = false;
    this.#seen = [...unseen.map((item) => item.id), ...this.#seen].slice(0, SEEN_MAX);
    this.#save();
    return show.reverse();
  }

  #load(): string[] | null {
    try {
      const raw = this.#storage?.getItem(SEEN_KEY);
      if (raw == null) return null;
      const parsed = JSON.parse(raw) as unknown;
      return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : [];
    } catch {
      return null;
    }
  }

  #save(): void {
    try {
      this.#storage?.setItem(SEEN_KEY, JSON.stringify(this.#seen));
    } catch {
      // storage blocked: duplicates are possible after a restart, nothing else
    }
  }
}
