// @vitest-environment node
/** Demo sandboxes (ADR 0076): the visitor flag, the automatic restart and the refusals. */
import { describe, expect, it } from 'vitest';

import {
  BUSY_RETRY_FALLBACK_S,
  DEMO_FULL_PROBLEM,
  FULL_RETRY_FALLBACK_S,
  TOO_MANY_PROBLEM,
  VISITOR_KEY,
  clock,
  demoRefusal,
  forgetVisitor,
  isVisitor,
  markVisitor,
  shouldRecreate,
  spokenMinutes,
  type AutoStartInput,
  type VisitorStorage,
} from './sandbox';

function memory(): VisitorStorage & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
    removeItem: (key) => void data.delete(key),
  };
}

const blocked: VisitorStorage = {
  getItem: () => {
    throw new Error('blocked');
  },
  setItem: () => {
    throw new Error('blocked');
  },
  removeItem: () => {
    throw new Error('blocked');
  },
};

describe('visitor flag', () => {
  it('is set by a demo start and cleared by a logout', () => {
    const storage = memory();
    expect(isVisitor(storage)).toBe(false);
    markVisitor(storage);
    expect(storage.data.get(VISITOR_KEY)).toBe('1');
    expect(isVisitor(storage)).toBe(true);
    forgetVisitor(storage);
    expect(isVisitor(storage)).toBe(false);
    expect(storage.data.has(VISITOR_KEY)).toBe(false);
  });

  it('never throws without storage', () => {
    expect(() => markVisitor(blocked)).not.toThrow();
    expect(() => forgetVisitor(blocked)).not.toThrow();
    expect(isVisitor(blocked)).toBe(false);
    expect(isVisitor(null)).toBe(false);
    expect(() => markVisitor(null)).not.toThrow();
  });

  it('only counts the exact value', () => {
    const storage = memory();
    storage.setItem(VISITOR_KEY, 'yes');
    expect(isVisitor(storage)).toBe(false);
  });
});

describe('shouldRecreate', () => {
  const base: AutoStartInput = {
    sandbox: true,
    visitor: true,
    status: 'anonymous',
    appRequest: false,
    tried: false,
  };

  it('restarts a lost sandbox of a visitor once', () => {
    expect(shouldRecreate(base)).toBe(true);
    expect(shouldRecreate({ ...base, tried: true })).toBe(false);
  });

  it('waits for the session and never runs over a live one', () => {
    expect(shouldRecreate({ ...base, status: 'unknown' })).toBe(false);
    expect(shouldRecreate({ ...base, status: 'authenticated' })).toBe(false);
  });

  it('needs sandboxes, the visitor flag and no app hand-off', () => {
    expect(shouldRecreate({ ...base, sandbox: false })).toBe(false);
    expect(shouldRecreate({ ...base, visitor: false })).toBe(false);
    expect(shouldRecreate({ ...base, appRequest: true })).toBe(false);
  });
});

describe('demoRefusal', () => {
  it('reads a full demo with its Retry-After', () => {
    expect(demoRefusal({ type: DEMO_FULL_PROBLEM, status: 503, retryAfter: 45 })).toEqual({
      kind: 'full',
      seconds: 45,
    });
  });

  it('falls back to 120 s for a full demo without a usable Retry-After', () => {
    for (const retryAfter of [null, undefined, 0, -3]) {
      expect(demoRefusal({ type: DEMO_FULL_PROBLEM, status: 503, retryAfter })).toEqual({
        kind: 'full',
        seconds: FULL_RETRY_FALLBACK_S,
      });
    }
    expect(FULL_RETRY_FALLBACK_S).toBe(120);
  });

  it('reads too many sandboxes from one address', () => {
    expect(demoRefusal({ type: TOO_MANY_PROBLEM, status: 429, retryAfter: 900 })).toEqual({
      kind: 'busy',
      seconds: 900,
    });
    expect(demoRefusal({ type: 'about:blank', status: 429, retryAfter: null })).toEqual({
      kind: 'busy',
      seconds: BUSY_RETRY_FALLBACK_S,
    });
  });

  it('leaves every other failure to the page', () => {
    expect(demoRefusal({ type: 'about:blank', status: 503, retryAfter: 10 })).toBeNull();
    expect(demoRefusal({ type: 'urn:mowdeck:problem:not-found', status: 404 })).toBeNull();
    expect(demoRefusal({ type: 'urn:mowdeck:problem:network', status: 0 })).toBeNull();
  });
});

describe('countdown', () => {
  it('shows mm:ss', () => {
    expect(clock(120)).toBe('02:00');
    expect(clock(61)).toBe('01:01');
    expect(clock(9.2)).toBe('00:10');
    expect(clock(0)).toBe('00:00');
    expect(clock(-4)).toBe('00:00');
    expect(clock(3600)).toBe('60:00');
  });

  it('is spoken in started minutes only', () => {
    expect(spokenMinutes(120)).toBe(2);
    expect(spokenMinutes(119)).toBe(2);
    expect(spokenMinutes(61)).toBe(2);
    expect(spokenMinutes(60)).toBe(1);
    expect(spokenMinutes(3)).toBe(1);
    expect(spokenMinutes(0)).toBe(1);
  });
});
