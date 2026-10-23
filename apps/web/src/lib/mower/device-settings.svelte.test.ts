import { describe, expect, it, vi } from 'vitest';

import type { SettingSpec } from './device-settings';
import { CONFIRM_MS, DeviceSettingsFlow, SAVED_SHOW_MS } from './device-settings.svelte';

const rain = { key: 'rain_detection', type: 'bool', group: 'weather', options: [] } as SettingSpec;

function setup(send: (command: unknown) => Promise<string> = async () => 'cmd-1') {
  const outcomes = new Map<string, { status: string; reason: string | null }>();
  let now = 1_000;
  const api = {
    send: vi.fn(send),
    outcome: (id: string) => outcomes.get(id),
    problemText: (error: unknown) => `refused: ${String(error)}`,
  };
  const flow = new DeviceSettingsFlow(api, () => now);
  return {
    flow,
    api,
    outcomes,
    tick: (ms: number) => (now += ms),
    now: () => now,
  };
}

describe('DeviceSettingsFlow', () => {
  it('shows the reported value while nothing changes', () => {
    const { flow, now } = setup();
    expect(flow.view('rain_detection', true, now())).toEqual({
      value: true,
      state: 'idle',
      reason: null,
    });
  });

  it('shows a change at once, then the mower value with "saved", then idle', async () => {
    const { flow, api, outcomes, tick, now } = setup();
    const sent = flow.change(rain, false, true);
    expect(flow.view('rain_detection', true, now())).toMatchObject({
      value: false,
      state: 'sending',
    });
    await sent;
    expect(api.send).toHaveBeenCalledWith({
      kind: 'set_setting',
      key: 'rain_detection',
      value: false,
    });
    outcomes.set('cmd-1', { status: 'accepted', reason: null });
    expect(flow.busy('rain_detection', true, now())).toBe(true);
    outcomes.set('cmd-1', { status: 'executed', reason: null });
    // Executed, but the mower has not reported the value yet: still on its way.
    expect(flow.view('rain_detection', true, now()).state).toBe('sending');
    expect(flow.view('rain_detection', false, now())).toEqual({
      value: false,
      state: 'saved',
      reason: null,
    });
    tick(SAVED_SHOW_MS + 1);
    expect(flow.view('rain_detection', false, now()).state).toBe('idle');
  });

  it('falls back to the mower value with the reason when the command fails', async () => {
    const { flow, outcomes, now } = setup();
    await flow.change(rain, false, true);
    outcomes.set('cmd-1', { status: 'rejected', reason: 'the mower is updating' });
    expect(flow.view('rain_detection', true, now())).toEqual({
      value: true,
      state: 'failed',
      reason: 'the mower is updating',
    });
  });

  it('names a refused request', async () => {
    const { flow, now } = setup(async () => {
      throw new Error('403');
    });
    await flow.change(rain, false, true);
    expect(flow.view('rain_detection', true, now())).toEqual({
      value: true,
      state: 'failed',
      reason: 'refused: Error: 403',
    });
  });

  it('says when an executed change is never reported', async () => {
    const { flow, outcomes, tick, now } = setup();
    await flow.change(rain, false, true);
    outcomes.set('cmd-1', { status: 'executed', reason: null });
    tick(CONFIRM_MS + 1);
    expect(flow.view('rain_detection', true, now())).toMatchObject({
      value: true,
      state: 'unconfirmed',
    });
  });

  it('does not send an unchanged value and keeps the newest change', async () => {
    let answer = 0;
    const { flow, api, now } = setup(async () => `cmd-${++answer}`);
    await flow.change(rain, true, true);
    expect(api.send).not.toHaveBeenCalled();
    const first = flow.change(rain, false, true);
    const second = flow.change(rain, true, false);
    await Promise.all([first, second]);
    expect(flow.view('rain_detection', true, now()).value).toBe(true);
    flow.reset();
    expect(flow.view('rain_detection', false, now()).state).toBe('idle');
  });
});
