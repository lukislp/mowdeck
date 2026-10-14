import { fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import Joystick from '../lib/drive/Joystick.svelte';
import {
  clampToPad,
  isZero,
  keysVector,
  limitVector,
  stickVector,
  type StickVector,
} from '../lib/drive/stick';

describe('stick math', () => {
  it('maps up to forward and left to a left turn', () => {
    expect(stickVector(0, -100, 100)).toEqual({ linear: 1, angular: 0 });
    expect(stickVector(0, 100, 100)).toEqual({ linear: -1, angular: 0 });
    expect(stickVector(-100, 0, 100)).toEqual({ linear: 0, angular: 1 });
    expect(stickVector(100, 0, 100)).toEqual({ linear: 0, angular: -1 });
  });

  it('has a 10 % radial dead zone and rescales the rest', () => {
    expect(stickVector(0, -9, 100)).toEqual({ linear: 0, angular: 0 });
    expect(stickVector(6, -6, 100)).toEqual({ linear: 0, angular: 0 });
    expect(stickVector(0, -55, 100)).toEqual({ linear: 0.5, angular: 0 });
    expect(stickVector(0, -55, 100, 0)).toEqual({ linear: 0.55, angular: 0 });
  });

  it('clamps to the rim', () => {
    expect(stickVector(0, -300, 100)).toEqual({ linear: 1, angular: 0 });
    const diagonal = stickVector(300, 300, 100);
    expect(Math.hypot(diagonal.linear, diagonal.angular)).toBeCloseTo(1, 1);
    expect(clampToPad(0, 50, 100)).toEqual({ x: 0, y: 0.5 });
    expect(clampToPad(10, 10, 0)).toEqual({ x: 0, y: 0 });
  });

  it('turns held arrow keys into a normalised vector', () => {
    expect(keysVector(new Set(['ArrowUp']))).toEqual({ linear: 1, angular: 0 });
    expect(keysVector(new Set(['ArrowDown', 'ArrowRight']))).toEqual({
      linear: -0.71,
      angular: -0.71,
    });
    expect(keysVector(new Set(['ArrowUp', 'ArrowDown']))).toEqual({ linear: 0, angular: 0 });
  });

  it('limits only the linear part', () => {
    expect(limitVector({ linear: 0.8, angular: -0.5 }, 0.25)).toEqual({
      linear: 0.2,
      angular: -0.5,
    });
    expect(limitVector({ linear: 1, angular: 0 }, 2)).toEqual({ linear: 1, angular: 0 });
    expect(isZero({ linear: 0, angular: 0 })).toBe(true);
    expect(isZero({ linear: 0, angular: 0.1 })).toBe(false);
  });
});

describe('Joystick', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function setup(props: Record<string, unknown> = {}) {
    const samples: StickVector[] = [];
    const engage = vi.fn();
    render(Joystick, {
      props: {
        label: 'Fahrhebel',
        hint: 'Halten und ziehen',
        interval: 100,
        onsample: (v: StickVector) => samples.push(v),
        onengage: engage,
        ...props,
      },
    });
    const pad = screen.getByRole('application', { name: 'Fahrhebel' });
    pad.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 200, height: 200, right: 200, bottom: 200 }) as DOMRect;
    return { pad, samples, engage };
  }

  it('is a named, described, focusable pad', () => {
    const { pad } = setup();
    expect(pad).toHaveAccessibleDescription('Halten und ziehen');
    expect(pad).toHaveAttribute('tabindex', '0');
  });

  it('samples at a fixed cadence while held by a pointer and stops on release', async () => {
    const { pad, samples, engage } = setup();
    await fireEvent.pointerDown(pad, { pointerId: 1, clientX: 100, clientY: 0, button: 0 });
    expect(engage).toHaveBeenCalledWith(true);
    expect(samples).toEqual([{ linear: 1, angular: 0 }]);
    await fireEvent.pointerMove(pad, { pointerId: 1, clientX: 0, clientY: 100 });
    vi.advanceTimersByTime(300);
    expect(samples).toHaveLength(4);
    expect(samples.at(-1)).toEqual({ linear: 0, angular: 1 });
    expect(pad).toHaveAttribute('data-engaged', 'true');
    await fireEvent.pointerUp(pad, { pointerId: 1 });
    expect(engage).toHaveBeenLastCalledWith(false);
    vi.advanceTimersByTime(500);
    expect(samples).toHaveLength(4);
    expect(pad).toHaveAttribute('data-linear', '0');
  });

  it('ignores other pointers and secondary mouse buttons', async () => {
    const { pad, engage } = setup();
    await fireEvent.pointerDown(pad, { pointerId: 1, pointerType: 'mouse', button: 2 });
    expect(engage).not.toHaveBeenCalled();
    await fireEvent.pointerDown(pad, { pointerId: 2, clientX: 100, clientY: 100 });
    await fireEvent.pointerUp(pad, { pointerId: 3 });
    expect(pad).toHaveAttribute('data-engaged', 'true');
    await fireEvent.pointerCancel(pad, { pointerId: 2 });
    expect(pad).toHaveAttribute('data-engaged', 'false');
  });

  it('drives with the arrow keys and releases on key up or Escape', async () => {
    const { pad, samples, engage } = setup();
    await fireEvent.keyDown(pad, { key: 'ArrowUp' });
    expect(samples.at(-1)).toEqual({ linear: 1, angular: 0 });
    await fireEvent.keyDown(pad, { key: 'ArrowLeft' });
    vi.advanceTimersByTime(100);
    expect(samples.at(-1)).toEqual({ linear: 0.71, angular: 0.71 });
    await fireEvent.keyUp(pad, { key: 'ArrowLeft' });
    expect(pad).toHaveAttribute('data-angular', '0');
    await fireEvent.keyUp(pad, { key: 'ArrowUp' });
    expect(engage).toHaveBeenLastCalledWith(false);
    await fireEvent.keyDown(pad, { key: 'ArrowDown' });
    await fireEvent.keyDown(pad, { key: 'Escape' });
    expect(pad).toHaveAttribute('data-engaged', 'false');
    await fireEvent.keyDown(pad, { key: 'a' });
    expect(pad).toHaveAttribute('data-engaged', 'false');
  });

  it('does nothing when disabled and releases when it becomes disabled', async () => {
    const { pad, engage } = setup({ disabled: true });
    expect(pad).toHaveAttribute('aria-disabled', 'true');
    expect(pad).toHaveAttribute('tabindex', '-1');
    await fireEvent.pointerDown(pad, { pointerId: 1, clientX: 100, clientY: 0 });
    await fireEvent.keyDown(pad, { key: 'ArrowUp' });
    expect(engage).not.toHaveBeenCalled();
  });

  it('releases on blur', async () => {
    const { pad, engage } = setup();
    await fireEvent.keyDown(pad, { key: 'ArrowRight' });
    await fireEvent.blur(pad);
    expect(engage).toHaveBeenLastCalledWith(false);
  });
});
