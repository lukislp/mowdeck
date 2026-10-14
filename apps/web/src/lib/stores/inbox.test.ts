import { describe, expect, it, vi } from 'vitest';

import {
  InboxStore,
  POLL_MS,
  type InboxApi,
  type InboxEnvironment,
  type InboxItem,
} from './inbox.svelte';

function item(id: string, read = false): InboxItem {
  return {
    id,
    mower_id: 'm1',
    kind: 'mowing_finished',
    severity: 'info',
    title: `Title ${id}`,
    body: 'Body',
    url: '/mowers/m1',
    occurred_at: '2026-10-09T12:00:00Z',
    created_at: '2026-10-09T12:00:01Z',
    read,
    read_at: read ? '2026-10-09T12:01:00Z' : null,
    acked: false,
    acked_at: null,
  };
}

function fakeApi(items: InboxItem[]): InboxApi & {
  list: ReturnType<typeof vi.fn>;
  read: ReturnType<typeof vi.fn>;
  ack: ReturnType<typeof vi.fn>;
} {
  return {
    list: vi.fn(async () => ({ items, unread: items.filter((i) => !i.read).length })),
    read: vi.fn(async (id: string) => {
      const found = items.find((i) => i.id === id)!;
      Object.assign(found, { read: true, read_at: '2026-10-09T12:02:00Z' });
      return { ...found };
    }),
    ack: vi.fn(async (id: string) => {
      const found = items.find((i) => i.id === id)!;
      Object.assign(found, {
        read: true,
        read_at: found.read_at ?? '2026-10-09T12:03:00Z',
        acked: true,
        acked_at: '2026-10-09T12:03:00Z',
      });
      return { ...found };
    }),
  };
}

function fakeEnvironment() {
  let tick: (() => void) | null = null;
  let back: (() => void) | null = null;
  const env: InboxEnvironment & { cleared: number; unsubscribed: number } = {
    cleared: 0,
    unsubscribed: 0,
    setInterval: (handler, ms) => {
      expect(ms).toBe(POLL_MS);
      tick = handler;
      return 1;
    },
    clearInterval: () => {
      env.cleared += 1;
      tick = null;
    },
    onReturn: (handler) => {
      back = handler;
      return () => {
        env.unsubscribed += 1;
        back = null;
      };
    },
  };
  return { env, tick: () => tick?.(), back: () => back?.() };
}

describe('InboxStore', () => {
  it('acknowledges an alarm once and counts it as read', async () => {
    const api = fakeApi([{ ...item('a'), kind: 'mower_lifted' }, item('b', true)]);
    const store = new InboxStore(api, fakeEnvironment().env);
    await store.refresh();
    expect(store.unread).toBe(1);
    await store.acknowledge('a');
    expect(store.items[0]).toMatchObject({ acked: true, read: true });
    expect(store.unread).toBe(0);
    await store.acknowledge('a');
    await store.acknowledge('missing');
    expect(api.ack).toHaveBeenCalledTimes(1);
    await store.acknowledge('b');
    expect(store.unread).toBe(0);
  });

  it('loads items and the unread count', async () => {
    const api = fakeApi([item('a'), item('b', true)]);
    const store = new InboxStore(api, fakeEnvironment().env);
    await store.refresh();
    expect(store.loaded).toBe(true);
    expect(store.items.map((i) => i.id)).toEqual(['a', 'b']);
    expect(store.unread).toBe(1);
  });

  it('shares concurrent refreshes and keeps the last items on errors', async () => {
    const api = fakeApi([item('a')]);
    const store = new InboxStore(api, fakeEnvironment().env);
    await Promise.all([store.refresh(), store.refresh()]);
    expect(api.list).toHaveBeenCalledTimes(1);
    api.list.mockRejectedValueOnce(new Error('down'));
    await store.refresh();
    expect(store.error).toBeInstanceOf(Error);
    expect(store.items).toHaveLength(1);
    await store.refresh();
    expect(store.error).toBeNull();
  });

  it('marks one item read and counts down at once', async () => {
    const api = fakeApi([item('a'), item('b')]);
    const store = new InboxStore(api, fakeEnvironment().env);
    await store.refresh();
    await store.markRead('a');
    expect(store.unread).toBe(1);
    expect(store.items[0]!.read).toBe(true);
    await store.markRead('a'); // already read: no second call
    await store.markRead('missing');
    expect(api.read).toHaveBeenCalledTimes(1);
  });

  it('marks everything read', async () => {
    const api = fakeApi([item('a'), item('b'), item('c', true)]);
    const store = new InboxStore(api, fakeEnvironment().env);
    await store.refresh();
    await store.markAllRead();
    expect(api.read).toHaveBeenCalledTimes(2);
    expect(store.unread).toBe(0);
    expect(store.items.every((i) => i.read)).toBe(true);
  });

  it('polls every minute and on return while started, reference counted', async () => {
    const api = fakeApi([item('a')]);
    const fake = fakeEnvironment();
    const store = new InboxStore(api, fake.env);
    const stopA = store.start();
    const stopB = store.start();
    await Promise.resolve();
    expect(api.list).toHaveBeenCalledTimes(1);
    fake.tick();
    await Promise.resolve();
    fake.back();
    await Promise.resolve();
    expect(api.list.mock.calls.length).toBeGreaterThanOrEqual(2);
    stopA();
    stopA();
    expect(fake.env.cleared).toBe(0);
    stopB();
    expect(fake.env.cleared).toBe(1);
    expect(fake.env.unsubscribed).toBe(1);
    store.clear();
    expect(store.items).toEqual([]);
    expect(store.unread).toBe(0);
    expect(store.loaded).toBe(false);
  });
});
