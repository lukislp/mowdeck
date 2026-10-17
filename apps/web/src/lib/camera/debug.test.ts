import { describe, expect, it } from 'vitest';

import {
  applyDebugQuery,
  describeExperiments,
  EXPERIMENTS_KEY,
  loadExperiments,
  parseExperiments,
} from './debug';
import { DEBUG_KEY } from './options';

class MemoryStorage {
  data = new Map<string, string>();
  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
  removeItem(key: string): void {
    this.data.delete(key);
  }
}

describe('camera debug switches', () => {
  it('ignores a query without cam switches', () => {
    const storage = new MemoryStorage();
    expect(applyDebugQuery('?tab=map', storage)).toBe(false);
    expect(storage.data.size).toBe(0);
  });

  it('turns debugging on and stores the experiments', () => {
    const storage = new MemoryStorage();
    expect(
      applyDebugQuery(
        '?camdebug=1&camcodec=h264&camclone=1&camparam=ENABLE_ENCODED_TRANSFORM=false&camparam=FOO=12&camparam=bad-name=1',
        storage,
      ),
    ).toBe(true);
    expect(storage.getItem(DEBUG_KEY)).toBe('1');
    const experiments = loadExperiments(storage);
    expect(experiments).toEqual({
      codec: 'h264',
      clone: true,
      fakeVideo: false,
      params: { ENABLE_ENCODED_TRANSFORM: false, FOO: 12 },
    });
    expect(describeExperiments(experiments)).toBe(
      'codec=h264 clone ENABLE_ENCODED_TRANSFORM=false FOO=12',
    );
    // Changes keep the rest; an empty camparam clears the parameters.
    applyDebugQuery('?camcodec=vp8&camparam=', storage);
    expect(loadExperiments(storage)).toEqual({
      codec: 'vp8',
      clone: true,
      fakeVideo: false,
      params: {},
    });
  });

  it('runs no experiment unless debugging is on', () => {
    const storage = new MemoryStorage();
    storage.setItem(EXPERIMENTS_KEY, JSON.stringify({ codec: 'h264', clone: true }));
    expect(loadExperiments(storage)).toEqual({
      codec: null,
      clone: false,
      fakeVideo: false,
      params: {},
    });
    expect(describeExperiments(loadExperiments(storage))).toBe('none');
  });

  it('plays the test pattern through the video on request', () => {
    const storage = new MemoryStorage();
    applyDebugQuery('?camdebug=1&camfakevideo=1', storage);
    expect(loadExperiments(storage).fakeVideo).toBe(true);
    expect(describeExperiments(loadExperiments(storage))).toBe('fakevideo');
  });

  it('camdebug=0 turns debugging and every experiment off', () => {
    const storage = new MemoryStorage();
    applyDebugQuery('?camdebug=1&camclone=1', storage);
    applyDebugQuery('?camdebug=0', storage);
    expect(storage.getItem(DEBUG_KEY)).toBeNull();
    expect(storage.getItem(EXPERIMENTS_KEY)).toBeNull();
  });

  it('parses untrusted JSON field by field', () => {
    expect(parseExperiments(null)).toEqual({
      codec: null,
      clone: false,
      fakeVideo: false,
      params: {},
    });
    expect(
      parseExperiments({ codec: 'av1', clone: 'yes', params: { OK: 'x', lower: 1, OBJ: {} } }),
    ).toEqual({ codec: null, clone: false, fakeVideo: false, params: { OK: 'x' } });
    // Broken storage: no experiments.
    const storage = new MemoryStorage();
    storage.setItem(DEBUG_KEY, '1');
    storage.setItem(EXPERIMENTS_KEY, '{broken');
    expect(loadExperiments(storage)).toEqual({
      codec: null,
      clone: false,
      fakeVideo: false,
      params: {},
    });
  });
});
