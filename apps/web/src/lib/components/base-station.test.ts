import { fireEvent, render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { setLocale } from '$lib/paraglide/runtime';

import BaseStationFirmware from './BaseStationFirmware.svelte';
import BaseStationStatus from './BaseStationStatus.svelte';

beforeEach(() => setLocale('de', { reload: false }));

const NOW = Date.parse('2026-10-23T12:00:10Z');

describe('BaseStationStatus', () => {
  it('shows link, reception, satellites, solution, Wi-Fi, position and who reported', () => {
    render(BaseStationStatus, {
      props: {
        state: {
          correction_link: 'lora',
          link_up: true,
          lora_channel: 7,
          satellites: 32,
          rtk_status: 'fixed',
          wifi_dbm: -61,
          position: { lat: 48.1371306, lon: 11.5760904 },
          moved: false,
          details_at: '2026-10-23T12:00:00Z',
        },
        mowerName: 'Luba',
        now: NOW,
      },
    });
    expect(screen.getByTestId('bs-correction').textContent).toContain('LoRa-Funk');
    expect(screen.getByTestId('bs-correction').textContent).toContain('Kanal 7');
    expect(screen.getByTestId('bs-link').dataset.up).toBe('true');
    expect(screen.getByTestId('bs-link').textContent).toContain('empfangen');
    expect(screen.getByTestId('bs-satellites').textContent).toContain('32');
    expect(screen.getByTestId('bs-solution').textContent).toContain('RTK Fix');
    expect(screen.getByTestId('bs-wifi').textContent).toContain('-61');
    expect(screen.getByTestId('bs-position').textContent).toContain('48.137131, 11.576090');
    expect(screen.getByTestId('bs-reported-by').textContent).toContain('über Luba');
    expect(screen.queryByTestId('bs-moved')).toBeNull();
  });

  it('warns when the base was moved and says when it waits', () => {
    const { unmount } = render(BaseStationStatus, {
      props: {
        state: { moved: true, link_up: false, correction_link: 'lora', rtk_status: 'unknown' },
        mowerName: null,
        now: NOW,
      },
    });
    expect(screen.getByTestId('bs-moved')).toBeTruthy();
    expect(screen.getByTestId('bs-link').textContent).toContain('getrennt');
    unmount();
    render(BaseStationStatus, { props: { state: null, mowerName: 'Luba', now: NOW } });
    expect(screen.getByTestId('bs-state-empty').textContent).toContain('über Luba');
  });
});

describe('BaseStationFirmware', () => {
  const base = {
    info: { installed: '1.0', available: '1.1', update_state: 'available' as const },
    reported: null,
    relays: [
      { id: 'm1', name: 'Busy', blocker: 'mower_busy' as const },
      { id: 'm2', name: 'Resting', blocker: null },
    ],
    blocker: null,
    checking: false,
    request: null,
    now: NOW,
  };

  it('installs through the first resting mower after the confirmation', async () => {
    const oninstall = vi.fn();
    render(BaseStationFirmware, { props: { ...base, oncheck: vi.fn(), oninstall } });
    expect(screen.getByTestId('bs-fw-installed').textContent).toContain('1.0');
    expect(screen.getByTestId('bs-fw-available').textContent).toContain('1.1');
    await fireEvent.click(screen.getByTestId('bs-fw-install'));
    expect(screen.getByTestId('bs-fw-confirm').textContent).toContain('keine RTK-Korrekturen');
    await fireEvent.click(screen.getByTestId('bs-fw-go'));
    expect(oninstall).toHaveBeenCalledWith('1.1', 'm2');
  });

  it('disables the install with the reason when no mower may relay', () => {
    render(BaseStationFirmware, {
      props: {
        ...base,
        relays: [{ id: 'm1', name: 'Busy', blocker: 'mower_busy' as const }],
        oncheck: vi.fn(),
        oninstall: vi.fn(),
      },
    });
    expect(screen.getByTestId('bs-fw-install')).toHaveProperty('disabled', true);
    expect(screen.getByTestId('bs-fw-blocker').dataset.reason).toBe('mower_busy');
  });

  it('needs a mower for the check and shows the request and the progress', async () => {
    const oncheck = vi.fn();
    const { unmount } = render(BaseStationFirmware, {
      props: { ...base, info: null, relays: [], oncheck, oninstall: vi.fn() },
    });
    expect(screen.getByTestId('bs-fw-check')).toHaveProperty('disabled', true);
    expect(screen.getByTestId('bs-fw-blocker').dataset.reason).toBe('no_mower');
    unmount();
    render(BaseStationFirmware, {
      props: {
        ...base,
        info: { installed: '1.0', update_state: 'installing' as const, progress: 40 },
        request: { status: 'accepted' as const, reason: 'update started' },
        oncheck,
        oninstall: vi.fn(),
      },
    });
    expect(screen.getByTestId('bs-fw-progress').textContent).toContain('40');
    expect(screen.getByTestId('bs-fw-request').textContent).toContain('update started');
    await fireEvent.click(screen.getByTestId('bs-fw-check'));
    expect(oncheck).toHaveBeenCalled();
  });
});
