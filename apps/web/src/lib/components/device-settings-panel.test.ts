import { fireEvent, render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { SettingSpec } from '$lib/mower/device-settings';
import { DeviceSettingsFlow } from '$lib/mower/device-settings.svelte';
import { setLocale } from '$lib/paraglide/runtime';

import DeviceSettingsPanel from './DeviceSettingsPanel.svelte';

beforeEach(() => setLocale('de', { reload: false }));

const SPECS = [
  { key: 'rain_detection', type: 'bool', group: 'weather', options: [] },
  { key: 'recharge_level', type: 'int', group: 'battery', min: 15, max: 30, step: 1, unit: '%' },
  {
    key: 'wildlife_safety',
    type: 'enum',
    group: 'safety',
    options: ['off', 'stop_mowing', 'low_speed_mowing'],
  },
  { key: 'voice_gender', type: 'enum', group: 'voice', options: ['man', 'woman', 'x', 'y'] },
  { key: 'non_work_hours', type: 'time_window', group: 'schedule', options: [] },
  { key: 'mulch_mode', type: 'bool', group: 'driving', options: [] },
] as SettingSpec[];

const VALUES = {
  rain_detection: true,
  recharge_level: 20,
  wildlife_safety: 'stop_mowing',
  voice_gender: 'woman',
  non_work_hours: { start: '22:00', end: '06:00' },
};

function flow(send = vi.fn(async () => 'cmd-1')) {
  return {
    send,
    flow: new DeviceSettingsFlow({
      send,
      outcome: () => undefined,
      problemText: () => 'refused',
    }),
  };
}

describe('DeviceSettingsPanel', () => {
  it('renders one translated row per declared setting, grouped', () => {
    render(DeviceSettingsPanel, {
      props: { specs: SPECS, values: VALUES, blocker: null, flow: flow().flow, now: 0 },
    });
    const groups = [...document.querySelectorAll('section[data-group]')].map(
      (el) => (el as HTMLElement).dataset.group,
    );
    expect(groups).toEqual(
      ['weather', 'battery', 'lights', 'driving', 'safety', 'voice', 'schedule'].filter(
        (g) => g !== 'lights',
      ),
    );
    expect(screen.getByTestId('setting-rain_detection').textContent).toContain('Regenerkennung');
    expect(screen.getByTestId('setting-rain_detection-switch').getAttribute('aria-checked')).toBe(
      'true',
    );
    expect(screen.getByTestId('setting-recharge_level').textContent).toContain('20');
    expect(screen.getByTestId('setting-wildlife_safety-choice').textContent).toContain(
      'Mähen stoppen',
    );
    // More than three options: a list instead of segments.
    expect(screen.queryByTestId('setting-voice_gender-choice')).toBeNull();
    expect((document.getElementById('setting-voice_gender-input') as HTMLSelectElement).value).toBe(
      'woman',
    );
    expect(screen.getByTestId('setting-non_work_hours-start')).toHaveProperty('value', '22:00');
    expect(screen.getByTestId('setting-non_work_hours').textContent).toContain('über Mitternacht');
    // An unknown key reads readable and says it was not reported yet.
    expect(screen.getByTestId('setting-mulch_mode').textContent).toContain('Mulch mode');
    expect(screen.getByTestId('setting-mulch_mode-unreported')).toBeTruthy();
  });

  it('sends a change and shows it at once', async () => {
    const { flow: f, send } = flow();
    render(DeviceSettingsPanel, {
      props: { specs: SPECS, values: VALUES, blocker: null, flow: f, now: 0 },
    });
    await fireEvent.click(screen.getByTestId('setting-rain_detection-switch'));
    expect(send).toHaveBeenCalledWith({ kind: 'set_setting', key: 'rain_detection', value: false });
    expect(screen.getByTestId('setting-rain_detection').dataset.state).toBe('sending');
    expect(screen.getByTestId('setting-rain_detection-note').textContent).toContain(
      'Wird übertragen',
    );
    await fireEvent.click(screen.getByRole('radio', { name: 'Langsam mähen' }));
    expect(send).toHaveBeenLastCalledWith({
      kind: 'set_setting',
      key: 'wildlife_safety',
      value: 'low_speed_mowing',
    });
  });

  it('switches the quiet hours off with null and applies a new window', async () => {
    const { flow: f, send } = flow();
    render(DeviceSettingsPanel, {
      props: { specs: SPECS, values: VALUES, blocker: null, flow: f, now: 0 },
    });
    const apply = screen.getByTestId('setting-non_work_hours-apply');
    expect(apply).toHaveProperty('disabled', true);
    await fireEvent.input(screen.getByTestId('setting-non_work_hours-end'), {
      target: { value: '07:15' },
    });
    expect(apply).toHaveProperty('disabled', false);
    await fireEvent.click(apply);
    expect(send).toHaveBeenLastCalledWith({
      kind: 'set_setting',
      key: 'non_work_hours',
      value: { start: '22:00', end: '07:15' },
    });
  });

  it('disables every control and says why', async () => {
    const { flow: f, send } = flow();
    render(DeviceSettingsPanel, {
      props: { specs: SPECS, values: VALUES, blocker: 'offline', flow: f, now: 0 },
    });
    expect(screen.getByTestId('device-settings-blocker').textContent).toContain('offline');
    expect(document.querySelector('fieldset')?.disabled).toBe(true);
    expect(screen.getByTestId('setting-rain_detection-switch')).toHaveProperty('disabled', true);
    await fireEvent.click(screen.getByTestId('setting-rain_detection-switch'));
    expect(send).not.toHaveBeenCalled();
  });
});
