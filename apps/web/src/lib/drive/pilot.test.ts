import { describe, expect, it } from 'vitest';

import type { DriveFrame } from '$lib/realtime/client.svelte';

import { DrivePilot, IDLE_FRAME_MS } from './pilot';

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
