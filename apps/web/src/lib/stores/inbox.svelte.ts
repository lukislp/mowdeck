/**
 * The user's notification inbox: the newest items and the unread count for the navigation
 * badge. Notifications has no Realtime message for new items, so the store polls every 60 s
 * while the app is open and refreshes when the window gets the focus or becomes visible again.
 * The API and the timers are injected for the tests.
 */
import type { components } from '#lib/generated/notifications.js';

export type InboxItem = components['schemas']['InboxItemOut'];

export const POLL_MS = 60_000;
export const INBOX_LIMIT = 50;

export interface InboxApi {
  list(limit: number): Promise<{ items: InboxItem[]; unread: number }>;
  read(id: string): Promise<InboxItem>;
  /** Read plus acknowledged (alarms); idempotent on the server. */
  ack(id: string): Promise<InboxItem>;
}

export interface InboxEnvironment {
  setInterval(handler: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
  /** Subscribe to "the user is back" (focus, visibility); returns the unsubscribe. */
  onReturn(handler: () => void): () => void;
}

const browserEnvironment: InboxEnvironment = {
  setInterval: (handler, ms) => setInterval(handler, ms),
  clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
  onReturn(handler) {
    if (typeof window === 'undefined') return () => undefined;
    const visible = () => {
      if (document.visibilityState === 'visible') handler();
    };
    window.addEventListener('focus', handler);
    document.addEventListener('visibilitychange', visible);
    return () => {
      window.removeEventListener('focus', handler);
      document.removeEventListener('visibilitychange', visible);
    };
  },
};

export class InboxStore {
  items = $state<InboxItem[]>([]);
  unread = $state(0);
  loaded = $state(false);
  error = $state<unknown>(null);

  readonly #api: InboxApi;
  readonly #env: InboxEnvironment;
  #users = 0;
  #stop: (() => void) | null = null;
  #inflight: Promise<void> | null = null;

  constructor(api: InboxApi, env: InboxEnvironment = browserEnvironment) {
    this.#api = api;
    this.#env = env;
  }

  /** Load the newest items (concurrent calls share one request). */
  refresh(): Promise<void> {
    this.#inflight ??= (async () => {
      try {
        const page = await this.#api.list(INBOX_LIMIT);
        this.items = page.items;
        this.unread = page.unread;
        this.loaded = true;
        this.error = null;
      } catch (error) {
        this.error = error;
      } finally {
        this.#inflight = null;
      }
    })();
    return this.#inflight;
  }

  /** Mark one item read (idempotent on the server); the count follows at once. */
  async markRead(id: string): Promise<void> {
    const item = this.items.find((i) => i.id === id);
    if (!item || item.read) return;
    const updated = await this.#api.read(id);
    this.items = this.items.map((i) => (i.id === id ? updated : i));
    this.unread = Math.max(0, this.unread - 1);
  }

  /** Acknowledge an alarm: the server marks it read and acknowledged and silences it an hour. */
  async acknowledge(id: string): Promise<void> {
    const item = this.items.find((i) => i.id === id);
    if (!item || item.acked) return;
    const wasUnread = !item.read;
    const updated = await this.#api.ack(id);
    this.items = this.items.map((i) => (i.id === id ? updated : i));
    if (wasUnread && updated.read) this.unread = Math.max(0, this.unread - 1);
  }

  /** Mark every loaded unread item read, then reload the count. */
  async markAllRead(): Promise<void> {
    const unread = this.items.filter((i) => !i.read);
    const done = await Promise.allSettled(unread.map((i) => this.#api.read(i.id)));
    const updated = new Map<string, InboxItem>();
    for (const result of done) {
      if (result.status === 'fulfilled') updated.set(result.value.id, result.value);
    }
    this.items = this.items.map((i) => updated.get(i.id) ?? i);
    await this.refresh();
  }

  /** Start polling (reference counted); the returned function stops this user's share. */
  start(): () => void {
    this.#users += 1;
    if (this.#users === 1) {
      void this.refresh();
      const timer = this.#env.setInterval(() => void this.refresh(), POLL_MS);
      const off = this.#env.onReturn(() => void this.refresh());
      this.#stop = () => {
        this.#env.clearInterval(timer);
        off();
      };
    }
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.#users -= 1;
      if (this.#users === 0) {
        this.#stop?.();
        this.#stop = null;
      }
    };
  }

  /** Forget everything (logout). */
  clear(): void {
    this.items = [];
    this.unread = 0;
    this.loaded = false;
    this.error = null;
  }
}
