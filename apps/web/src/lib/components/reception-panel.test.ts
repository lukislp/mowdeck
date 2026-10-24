import { render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it } from 'vitest';

import ReceptionPanel from './ReceptionPanel.svelte';

import { setLocale } from '#lib/paraglide/runtime.js';

beforeEach(() => setLocale('de', { reload: false }));

const full = {
  gnss: { satellites: 29, correction_age_s: 1, rtk_status: 'fixed' as const },
  signal: { wifi_dbm: -58, mobile_dbm: -97, network: 'wifi' as const },
  selfCheck: { result: 'ok' as const },
  rainDetected: false,
};

describe('ReceptionPanel', () => {
  it('shows RTK, satellites, correction age, both radios, the uplink and the checks', () => {
    render(ReceptionPanel, { props: full });
    const solution = screen.getByTestId('rx-solution');
    expect(solution.textContent).toContain('RTK Fix');
    expect(solution.dataset.tone).toBe('mowing');
    expect(screen.getByTestId('rx-satellites').textContent).toContain('29');
    expect(screen.getByTestId('rx-correction').textContent).toContain('1,0 s');
    const wifi = screen.getByTestId('rx-wifi');
    expect(wifi.textContent).toContain('-58');
    expect(wifi.querySelector('.glyph')?.getAttribute('data-bars')).toBe('3');
    expect(wifi.querySelector('.glyph')?.getAttribute('aria-label')).toBe('WLAN: 3 von 4 Balken');
    expect(screen.getByTestId('rx-mobile').querySelector('.glyph')?.getAttribute('data-bars')).toBe(
      '2',
    );
    expect(screen.getByTestId('rx-network').textContent).toContain('WLAN');
    expect(screen.getByTestId('rx-self-check').dataset.result).toBe('ok');
    expect(screen.getByTestId('rx-self-check').textContent).toContain('Startbereit');
    expect(screen.queryByTestId('rx-self-check-reason')).toBeNull();
    expect(screen.getByTestId('rx-rain').textContent).toContain('Trocken');
  });

  it('names why the self-check blocks and that it rains', () => {
    render(ReceptionPanel, {
      props: {
        ...full,
        selfCheck: { result: 'blocked' as const, detail: 'rain' },
        rainDetected: true,
      },
    });
    expect(screen.getByTestId('rx-self-check').dataset.result).toBe('blocked');
    expect(screen.getByTestId('rx-self-check').textContent).toContain('Start blockiert');
    expect(screen.getByTestId('rx-self-check-reason').textContent).toContain('Regen erkannt');
    expect(screen.getByTestId('rx-rain').dataset.rain).toBe('wet');
  });

  it('leaves out what the mower does not report and dashes missing radios', () => {
    render(ReceptionPanel, {
      props: {
        gnss: null,
        signal: { wifi_dbm: null, mobile_dbm: null, network: null },
        selfCheck: null,
        rainDetected: null,
      },
    });
    expect(screen.queryByTestId('rx-gnss')).toBeNull();
    expect(screen.queryByTestId('rx-self-check')).toBeNull();
    expect(screen.queryByTestId('rx-rain')).toBeNull();
    expect(screen.getByTestId('rx-wifi').querySelector('.glyph')?.getAttribute('data-bars')).toBe(
      'none',
    );
    expect(screen.getByTestId('rx-network').textContent).toContain('keine Daten');
  });

  it('warns on stale corrections and a lost signal', () => {
    render(ReceptionPanel, {
      props: {
        ...full,
        gnss: { satellites: 7, correction_age_s: 45, rtk_status: 'single' as const },
        signal: { wifi_dbm: -92, mobile_dbm: null, network: 'mobile' as const },
      },
    });
    expect(screen.getByTestId('rx-solution').dataset.tone).toBe('paused');
    expect(screen.getByTestId('rx-correction').dataset.tone).toBe('paused');
    expect(screen.getByTestId('rx-wifi').querySelector('.glyph')?.getAttribute('data-tone')).toBe(
      'error',
    );
  });
});
