import { describe, expect, it } from 'vitest';

import { lightCapable, lightView, quietLightResult, setLight, type LightInput } from './light';

const base: LightInput = { on: false, capable: true, online: true, allowed: true, pending: false };

describe('lightView', () => {
  it('asks for the opposite of the reported state', () => {
    expect(lightView(base)).toEqual({ state: 'off', reason: null, disabled: false, next: true });
    expect(lightView({ ...base, on: true })).toMatchObject({ state: 'on', next: false });
  });

  it('switches an unreported light on', () => {
    expect(lightView({ ...base, on: null })).toMatchObject({ state: 'unknown', next: true });
  });

  it('waits while a command is on its way', () => {
    expect(lightView({ ...base, pending: true })).toMatchObject({ reason: null, disabled: true });
  });

  it.each([
    [{ capable: false }, 'unsupported'],
    [{ online: false }, 'offline'],
    [{ allowed: false }, 'no_permission'],
    [{ capable: false, online: false, allowed: false }, 'unsupported'],
    [{ online: false, allowed: false }, 'offline'],
  ] as const)('is disabled when %o (%s)', (override, reason) => {
    expect(lightView({ ...base, ...override })).toMatchObject({ reason, disabled: true });
  });
});

describe('lightCapable', () => {
  it('reads the capability flag only', () => {
    expect(lightCapable({ light: true })).toBe(true);
    expect(lightCapable({ light: false })).toBe(false);
    expect(lightCapable({})).toBe(false);
    expect(lightCapable(null)).toBe(false);
    expect(lightCapable(undefined)).toBe(false);
  });
});

describe('setLight', () => {
  it('builds the set_light command', () => {
    expect(setLight(true)).toEqual({ kind: 'set_light', on: true });
    expect(setLight(false)).toEqual({ kind: 'set_light', on: false });
  });
});

describe('quietLightResult', () => {
  const executed = { kind: 'set_light', status: 'executed', mowerId: 'm1' };

  it('keeps a switched light quiet while that mower is driven', () => {
    expect(quietLightResult(executed, 'm1')).toBe(true);
  });

  it('still reports failures, other mowers, other commands and outside a drive', () => {
    expect(quietLightResult({ ...executed, status: 'failed' }, 'm1')).toBe(false);
    expect(quietLightResult({ ...executed, status: 'rejected' }, 'm1')).toBe(false);
    expect(quietLightResult(executed, 'm2')).toBe(false);
    expect(quietLightResult({ ...executed, kind: 'set_speed' }, 'm1')).toBe(false);
    expect(quietLightResult(executed, null)).toBe(false);
  });
});
