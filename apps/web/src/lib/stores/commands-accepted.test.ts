import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ACCEPTED_IS_FINAL,
  CommandStore,
  POLL_INTERVAL_MS,
  RESULT_WAIT_MS,
  isFinal,
  type CommandApi,
} from './commands.svelte';

function api(statuses: string[] = []): CommandApi {
  let n = 0;
  return {
    post: vi.fn(async () => ({
      command_id: `c${++n}`,
      expires_at: new Date(Date.now() + 30_000).toISOString(),
    })),
    status: vi.fn(async () => ({ state: statuses.shift() ?? 'pending', reason: null })),
  };
}

describe('accepted as the final answer of maintenance commands (ADR 0048)', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('knows the kinds the mower does not confirm', () => {
    expect([...ACCEPTED_IS_FINAL].sort()).toEqual([
      'leave_dock',
      'relocate_dock',
      'restart_mower',
      'resync_rtk',
      'sync_map',
    ]);
    expect(isFinal('restart_mower', 'accepted')).toBe(true);
    expect(isFinal('start_mow', 'accepted')).toBe(false);
    expect(isFinal('start_mow', 'executed')).toBe(true);
    expect(isFinal('restart_mower', 'pending')).toBe(false);
  });

  it('ends a restart at accepted: no longer pending, final once, never expired', async () => {
    const store = new CommandStore(api());
    const final = vi.fn();
    store.onFinal(final);
    await store.send('m1', { kind: 'restart_mower' });
    store.resolve('c1', 'accepted', 'restart sent');
    expect(store.pending('m1', 'restart_mower')).toBe(false);
    expect(final).toHaveBeenCalledOnce();
    expect(final.mock.calls[0]![0]).toMatchObject({ status: 'accepted', reason: 'restart sent' });
    await vi.advanceTimersByTimeAsync(RESULT_WAIT_MS + 40 * POLL_INTERVAL_MS);
    expect(store.get('c1')?.status).toBe('accepted');
    expect(final).toHaveBeenCalledOnce();
  });

  it('keeps an accepted start pending until it is executed', async () => {
    const store = new CommandStore(api());
    await store.send('m1', { kind: 'start_mow' });
    store.resolve('c1', 'accepted');
    expect(store.pending('m1', 'start_mow')).toBe(true);
    store.resolve('c1', 'executed');
    expect(store.pending('m1', 'start_mow')).toBe(false);
  });

  it('stops polling when the record says accepted for a maintenance kind', async () => {
    const commands = api(['accepted']);
    const store = new CommandStore(commands);
    await store.send('m1', { kind: 'leave_dock' });
    await vi.advanceTimersByTimeAsync(RESULT_WAIT_MS + 1);
    expect(store.get('c1')?.status).toBe('accepted');
    await vi.advanceTimersByTimeAsync(10 * POLL_INTERVAL_MS);
    expect(commands.status).toHaveBeenCalledOnce();
  });
});
