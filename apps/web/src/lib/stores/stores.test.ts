import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CommandStore, POLL_INTERVAL_MS, RESULT_WAIT_MS, type CommandApi } from './commands.svelte';
import { MowerStore, STALE_AFTER_MS, type MowerState } from './mowers.svelte';

function state(observedAt: string, activity: MowerState['activity'] = 'docked'): MowerState {
  return {
    mower_id: 'm1',
    account_id: 'a1',
    observed_at: observedAt,
    activity,
    battery_percent: 80,
    connection: 'simulated',
  } as MowerState;
}

describe('MowerStore', () => {
  it('keeps the newest observed_at', () => {
    const store = new MowerStore(() => 0);
    expect(store.upsert('m1', state('2026-10-09T12:00:10Z', 'mowing'), 'live')).toBe(true);
    expect(store.upsert('m1', state('2026-10-09T12:00:05Z', 'docked'), 'rest')).toBe(false);
    expect(store.get('m1')?.state.activity).toBe('mowing');
    expect(store.upsert('m1', state('2026-10-09T12:00:20Z', 'paused'), 'live')).toBe(true);
    expect(store.get('m1')?.source).toBe('live');
  });

  it('flags stale states and forgets mowers', () => {
    const at = Date.parse('2026-10-09T12:00:00Z');
    const store = new MowerStore(() => at);
    store.upsert('m1', state('2026-10-09T12:00:00Z'), 'rest');
    expect(store.isStale('m1', at + STALE_AFTER_MS)).toBe(false);
    expect(store.isStale('m1', at + STALE_AFTER_MS + 1)).toBe(true);
    expect(store.isStale('unknown')).toBe(false);
    store.remove('m1');
    expect(store.has('m1')).toBe(false);
    store.upsert('m1', state('2026-10-09T12:00:00Z'), 'rest');
    store.clear();
    expect(store.get('m1')).toBeUndefined();
  });
});

describe('CommandStore', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function api(statuses: string[] = []): CommandApi & { post: ReturnType<typeof vi.fn> } {
    let n = 0;
    return {
      post: vi.fn(async () => ({
        command_id: `c${++n}`,
        expires_at: new Date(Date.now() + 30_000).toISOString(),
      })),
      status: vi.fn(async () => ({ state: statuses.shift() ?? 'accepted', reason: null })),
    };
  }

  it('tracks a command until the socket result arrives', async () => {
    const store = new CommandStore(api());
    const final = vi.fn();
    store.onFinal(final);
    const sent = await store.send('m1', { kind: 'pause_mow' });
    expect(sent.status).toBe('pending');
    expect(store.pending('m1', 'pause_mow')).toBe(true);
    store.resolve('c1', 'executed');
    expect(store.pending('m1', 'pause_mow')).toBe(false);
    expect(store.get('c1')?.status).toBe('executed');
    expect(final).toHaveBeenCalledOnce();
    store.resolve('c1', 'failed'); // final stays final
    expect(store.get('c1')?.status).toBe('executed');
    expect(store.latest('m1', 'pause_mow')?.id).toBe('c1');
  });

  it('accepts a result that arrives before the 202 response', async () => {
    const store = new CommandStore(api());
    store.resolve('c1', 'executed', 'done');
    const sent = await store.send('m1', { kind: 'emergency_stop' });
    expect(sent.status).toBe('executed');
  });

  it('polls the status when no result arrives in time', async () => {
    const backend = api(['accepted', 'executed']);
    const store = new CommandStore(backend);
    await store.send('m1', { kind: 'return_to_dock' });
    await vi.advanceTimersByTimeAsync(RESULT_WAIT_MS);
    expect(store.get('c1')?.status).toBe('accepted');
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(store.get('c1')?.status).toBe('executed');
    expect(backend.status).toHaveBeenCalledTimes(2);
  });

  it('expires a command whose result never comes', async () => {
    const store = new CommandStore(api(['pending', 'pending', 'pending']));
    await store.send('m1', { kind: 'start_mow', area_ids: ['front'] });
    await vi.advanceTimersByTimeAsync(40_000);
    expect(store.get('c1')?.status).toBe('expired');
  });

  it('maps unknown states to pending and clears', async () => {
    const store = new CommandStore(api());
    await store.send('m1', { kind: 'set_speed', mps: 0.4 });
    store.resolve('c1', 'something');
    expect(store.get('c1')?.status).toBe('pending');
    store.clear();
    expect(store.get('c1')).toBeUndefined();
  });
});
