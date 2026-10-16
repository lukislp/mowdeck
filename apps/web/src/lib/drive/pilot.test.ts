import { describe, expect, it } from 'vitest';

import type { DriveFrame } from '$lib/realtime/client.svelte';

import { DrivePilot, IDLE_FRAME_MS, clampReverse } from './pilot';

function setup(accept = true) {
  let now = 10_000;
  const frames: { frame: DriveFrame; force: boolean }[] = [];
  const pilot = new DrivePilot(
    {
      sendFrame: (frame, force = false) => {
        if (accept) frames.push({ frame, force });
        return accept;
      },
    },
    () => now,
  );
  return {
    pilot,
    frames,
    advance: (ms: number) => {
      now += ms;
    },
  };
}

describe('DrivePilot', () => {
  it('sends every deflected sample, scaled by the speed limit', () => {
    const { pilot, frames, advance } = setup();
    pilot.speedLimit = 50;
    expect(pilot.sample({ linear: 0.8, angular: -0.4 })).toBe(true);
    advance(100);
    pilot.sample({ linear: 1, angular: 0 });
    expect(frames.map((f) => f.frame)).toEqual([
      { linear: 0.4, angular: -0.4, blade_on: false },
      { linear: 0.5, angular: 0, blade_on: false },
    ]);
    expect(frames.every((f) => !f.force)).toBe(true);
  });

  it('keeps a held, centred stick alive at 2 Hz only', () => {
    const { pilot, frames, advance } = setup();
    pilot.sample({ linear: 1, angular: 0 });
    for (let i = 0; i < 10; i++) {
      advance(100);
      pilot.sample({ linear: 0, angular: 0 });
    }
    // One deflected frame, then a zero frame every 500 ms over the following second.
    expect(frames).toHaveLength(3);
    expect(frames.slice(1).map((f) => f.frame.linear)).toEqual([0, 0]);
    expect(IDLE_FRAME_MS).toBe(500);
  });

  it('sends one forced zero frame without blades on release', () => {
    const { pilot, frames } = setup();
    pilot.bladeOn = true;
    pilot.sample({ linear: 0.5, angular: 0 });
    expect(pilot.release()).toBe(true);
    expect(frames.at(0)?.frame.blade_on).toBe(true);
    expect(frames.at(-1)).toEqual({
      frame: { linear: 0, angular: 0, blade_on: false },
      force: true,
    });
  });

  it('counts sent and acknowledged frames of the last second', () => {
    const { pilot, advance } = setup();
    pilot.sample({ linear: 1, angular: 0 });
    pilot.acked();
    advance(400);
    pilot.sample({ linear: 1, angular: 0 });
    expect(pilot.rates()).toEqual({ sent: 2, acked: 1 });
    advance(700);
    expect(pilot.rates()).toEqual({ sent: 1, acked: 0 });
    pilot.reset();
    expect(pilot.rates()).toEqual({ sent: 0, acked: 0 });
    expect(pilot.bladeOn).toBe(false);
  });

  it('does not count frames the link refused', () => {
    const { pilot } = setup(false);
    expect(pilot.sample({ linear: 1, angular: 0 })).toBe(false);
    expect(pilot.release()).toBe(false);
    expect(pilot.rates().sent).toBe(0);
  });
});

describe('reverse driving', () => {
  it('clampReverse cuts the backwards part without reverse and keeps turning', () => {
    expect(clampReverse({ linear: -0.6, angular: 0.3 }, false)).toEqual({
      linear: 0,
      angular: 0.3,
    });
    expect(clampReverse({ linear: 0.6, angular: -0.3 }, false)).toEqual({
      linear: 0.6,
      angular: -0.3,
    });
    expect(clampReverse({ linear: -0.6, angular: 0.3 }, true)).toEqual({
      linear: -0.6,
      angular: 0.3,
    });
  });

  it('never sends a backwards frame over a link without reverse (the default)', () => {
    const { pilot, frames, advance } = setup();
    expect(pilot.reverseAllowed).toBe(false);
    pilot.speedLimit = 100;
    pilot.sample({ linear: -1, angular: 0.5 });
    advance(100);
    pilot.sample({ linear: -0.4, angular: -0.2 });
    expect(frames.map((f) => f.frame.linear)).toEqual([0, 0]);
    expect(frames[0]!.frame.angular).toBe(0.5);
    expect(frames.every((f) => f.frame.linear >= 0)).toBe(true);
  });

  it('sends backwards frames, scaled, when the link can reverse', () => {
    const { pilot, frames } = setup();
    pilot.reverseAllowed = true;
    pilot.speedLimit = 50;
    pilot.sample({ linear: -0.8, angular: 0 });
    expect(frames.at(0)?.frame.linear).toBe(-0.4);
  });
});

describe('hold while the picture stands', () => {
  it('stops at once and sends only zero frames, at the idle cadence, while held', () => {
    const { pilot, frames, advance } = setup();
    pilot.speedLimit = 100;
    pilot.sample({ linear: 0.8, angular: 0.2 });
    pilot.hold(true);
    expect(pilot.holding).toBe(true);
    expect(frames.at(-1)).toEqual({
      frame: { linear: 0, angular: 0, blade_on: false },
      force: true,
    });
    const before = frames.length;
    for (let i = 0; i < 10; i++) {
      advance(100);
      pilot.sample({ linear: 1, angular: -1 });
    }
    const held = frames.slice(before);
    // The stick is ignored: zero frames only, two in a second keep the session open.
    expect(held).toHaveLength(2);
    expect(held.every((f) => f.frame.linear === 0 && f.frame.angular === 0)).toBe(true);
    // A second hold(true) sends nothing extra.
    const count = frames.length;
    pilot.hold(true);
    expect(frames).toHaveLength(count);
  });

  it('never sends blades while holding', () => {
    const { pilot, frames, advance } = setup();
    pilot.bladeOn = true;
    pilot.hold(true);
    advance(600);
    pilot.sample({ linear: 0, angular: 0 });
    expect(frames.every((f) => f.frame.blade_on === false)).toBe(true);
  });

  it('keeps a held stick latched after the picture returns until it is centred', () => {
    const { pilot, frames, advance } = setup();
    pilot.speedLimit = 100;
    pilot.sample({ linear: 1, angular: 0 });
    pilot.hold(true);
    advance(100);
    pilot.sample({ linear: 1, angular: 0 });
    pilot.hold(false);
    expect(pilot.holding).toBe(true);
    for (let i = 0; i < 5; i++) {
      advance(600);
      pilot.sample({ linear: 1, angular: 0 });
    }
    expect(frames.slice(1).every((f) => f.frame.linear === 0)).toBe(true);
    // Centred: free again, the next deflection drives.
    advance(600);
    pilot.sample({ linear: 0, angular: 0 });
    expect(pilot.holding).toBe(false);
    advance(100);
    pilot.sample({ linear: 0.5, angular: 0 });
    expect(frames.at(-1)?.frame.linear).toBe(0.5);
  });

  it('frees a held stick that is let go and touched again', () => {
    const { pilot, frames, advance } = setup();
    pilot.speedLimit = 100;
    pilot.sample({ linear: 1, angular: 0 });
    pilot.hold(true);
    pilot.hold(false);
    advance(100);
    pilot.sample({ linear: 1, angular: 0 });
    expect(frames.at(-1)?.frame.linear).toBe(0);
    pilot.release();
    expect(pilot.holding).toBe(false);
    advance(100);
    pilot.sample({ linear: 0.7, angular: 0 });
    expect(frames.at(-1)?.frame.linear).toBe(0.7);
  });

  it('keeps the latch when the stick is let go during the hold and touched again before it ends', () => {
    const { pilot, frames, advance } = setup();
    pilot.speedLimit = 100;
    pilot.hold(true);
    pilot.release();
    expect(pilot.holding).toBe(true);
    advance(100);
    pilot.sample({ linear: 1, angular: 0 });
    pilot.hold(false);
    advance(600);
    pilot.sample({ linear: 1, angular: 0 });
    expect(frames.at(-1)?.frame.linear).toBe(0);
  });

  it('frees a stick nobody holds as soon as the picture returns', () => {
    const { pilot, frames, advance } = setup();
    pilot.speedLimit = 100;
    pilot.sample({ linear: 0.4, angular: 0 });
    pilot.release();
    pilot.hold(true);
    pilot.hold(false);
    expect(pilot.holding).toBe(false);
    advance(100);
    pilot.sample({ linear: 0.4, angular: 0 });
    expect(frames.at(-1)?.frame.linear).toBe(0.4);
  });

  it('a new session keeps holding while the picture still stands', () => {
    const { pilot, frames } = setup();
    pilot.hold(true);
    pilot.reset();
    expect(pilot.holding).toBe(true);
    pilot.sample({ linear: 1, angular: 0 });
    expect(frames.every((f) => f.frame.linear === 0)).toBe(true);
    // The stick was held through the end of the hold: latched until it is let go.
    pilot.hold(false);
    expect(pilot.holding).toBe(true);
    pilot.release();
    expect(pilot.holding).toBe(false);
  });
});
