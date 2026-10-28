import { fireEvent, render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { setLocale } from '#lib/paraglide/runtime.js';

import PositionFix from './PositionFix.svelte';

beforeEach(() => setLocale('de', { reload: false }));

const POINT = { lat: 52.5200081, lon: 13.4049542, at: '2026-10-28T12:00:00Z' };
const NOW = Date.parse('2026-10-28T12:00:30Z');

describe('PositionFix', () => {
  it('shows the coordinates as text with the time of the fix', () => {
    render(PositionFix, { props: { point: POINT, now: NOW } });
    const field = screen.getByTestId('position-fix-coordinates') as HTMLInputElement;
    expect(field.value).toBe('52.520008, 13.404954');
    expect(screen.getByTestId('position-fix').textContent).toContain('Letzte Position');
  });

  it('copies the coordinates', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(PositionFix, { props: { point: POINT, now: NOW } });
    await fireEvent.click(screen.getByTestId('position-fix-copy'));
    expect(writeText).toHaveBeenCalledWith('52.520008, 13.404954');
    expect(await screen.findByText('Kopiert')).toBeTruthy();
  });

  it('always offers Apple Maps, Google Maps and OpenStreetMap in a new tab without referrer', () => {
    render(PositionFix, { props: { point: POINT, now: NOW } });
    for (const [id, host] of [
      ['apple', 'https://maps.apple.com/'],
      ['google', 'https://www.google.com/maps/'],
      ['osm', 'https://www.openstreetmap.org/'],
    ] as const) {
      const link = screen.getByTestId(`position-fix-${id}`) as HTMLAnchorElement;
      expect(link.href.startsWith(host)).toBe(true);
      expect(link.target).toBe('_blank');
      expect(link.rel).toBe('noopener noreferrer');
    }
    expect(screen.getByTestId('position-fix-apple').textContent).toContain('Apple Karten');
    expect(screen.getByTestId('position-fix-google').textContent).toContain('Google Maps');
  });
});
