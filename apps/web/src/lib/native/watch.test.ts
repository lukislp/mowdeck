import { describe, expect, it } from 'vitest';

import type { NativePlugin } from './capacitor';
import { readWatchStatus, syncWatch, watchStateOf } from './watch';

function shell(answer: () => unknown): NativePlugin & { calls: string[] } {
  const calls: string[] = [];
  return {
    calls,
    async call(method) {
      calls.push(method);
      return answer() as never;
    },
    on() {
      return () => undefined;
    },
  };
}

describe('watchStateOf', () => {
  const ready = { supported: true, paired: true, installed: true, reachable: false };

  it('is ready with a paired watch that has the app and widgets on', () => {
    expect(watchStateOf(ready, 'on')).toBe('ready');
  });

  it('needs widgets: the watch relays through their credential (ADR 0060)', () => {
    expect(watchStateOf(ready, 'off')).toBe('needs-widgets');
    expect(watchStateOf(ready, 'error')).toBe('needs-widgets');
  });

  it('tells a missing watch from a missing app', () => {
    expect(watchStateOf({ ...ready, installed: false }, 'on')).toBe('not-installed');
    expect(watchStateOf({ ...ready, paired: false, installed: false }, 'on')).toBe('unpaired');
  });

  it('is unsupported without WatchConnectivity or an answer', () => {
    expect(watchStateOf({ supported: false }, 'on')).toBe('unsupported');
    expect(watchStateOf(null, 'on')).toBe('unsupported');
  });
});

describe('the shell calls', () => {
  it('reads the status and treats a failing or old shell as no status', async () => {
    const good = shell(() => ({ supported: true, paired: true }));
    expect(await readWatchStatus(good)).toEqual({ supported: true, paired: true });
    expect(good.calls).toEqual(['getWatchStatus']);
    const old = shell(() => {
      throw new Error('not implemented');
    });
    expect(await readWatchStatus(old)).toBeNull();
    expect(await readWatchStatus(null)).toBeNull();
  });

  it('asks the shell to send the state to the watch and never throws', async () => {
    const failing = shell(() => {
      throw new Error('no watch');
    });
    await expect(syncWatch(failing)).resolves.toBeUndefined();
    expect(failing.calls).toEqual(['syncWatch']);
    await expect(syncWatch(null)).resolves.toBeUndefined();
  });
});
