import { afterEach, describe, expect, it } from 'vitest';

import { STOP_CLEARANCE, STOP_INSET_START, publishStopClearance } from './stop-clearance';

const root = document.documentElement;

afterEach(() => {
  root.style.removeProperty(STOP_CLEARANCE);
  root.style.removeProperty(STOP_INSET_START);
});

describe('publishStopClearance', () => {
  it('publishes the height of a bottom bar, rounded up, and removes it again', () => {
    const remove = publishStopClearance({ bottom: 131.4 });
    expect(root.style.getPropertyValue(STOP_CLEARANCE)).toBe('132px');
    expect(root.style.getPropertyValue(STOP_INSET_START)).toBe('');
    remove();
    expect(root.style.getPropertyValue(STOP_CLEARANCE)).toBe('');
  });

  it('publishes the width of a rail instead of a bottom clearance', () => {
    root.style.setProperty(STOP_CLEARANCE, '120px');
    const remove = publishStopClearance({ insetStart: 112 });
    expect(root.style.getPropertyValue(STOP_INSET_START)).toBe('112px');
    // A rail leaves the bottom edge free: a stale bar height must not linger.
    expect(root.style.getPropertyValue(STOP_CLEARANCE)).toBe('');
    remove();
    expect(root.style.getPropertyValue(STOP_INSET_START)).toBe('');
  });

  it('ignores sizes that are not measured yet', () => {
    publishStopClearance({ bottom: 0, insetStart: Number.NaN });
    expect(root.style.getPropertyValue(STOP_CLEARANCE)).toBe('');
    expect(root.style.getPropertyValue(STOP_INSET_START)).toBe('');
  });

  it('writes to the element it is given', () => {
    const element = document.createElement('div');
    publishStopClearance({ bottom: 88 }, element);
    expect(element.style.getPropertyValue(STOP_CLEARANCE)).toBe('88px');
    expect(root.style.getPropertyValue(STOP_CLEARANCE)).toBe('');
  });
});
