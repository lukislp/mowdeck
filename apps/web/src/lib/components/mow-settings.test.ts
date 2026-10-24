import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import { NO_OPTIONS, type JobOptionsSpec } from '#lib/mower/job-options.js';
import { FALLBACK, offeredPatterns } from '#lib/mower/mow-pattern.js';
import { setLocale } from '#lib/paraglide/runtime.js';

import MowSettings from './MowSettings.svelte';

const ALL = offeredPatterns(['stripes', 'diagonal', 'checkerboard', 'edge_only', 'random_angle']);

function summary(): string {
  return screen.getByTestId('start-summary').textContent?.trim() ?? '';
}

describe('MowSettings', () => {
  it('offers the native patterns and summarises the choice', async () => {
    setLocale('de', { reload: false });
    render(MowSettings, {
      props: { offered: ALL, spacing: null, draft: { ...FALLBACK }, remember: true },
    });
    expect(screen.getByTestId('start-pattern').querySelectorAll('[role="radio"]')).toHaveLength(5);
    expect(screen.getByTestId('start-pattern-stripes').getAttribute('aria-checked')).toBe('true');
    expect(summary()).toBe('Streifen 0° · 1 Randrunde');
    expect(screen.queryByTestId('start-spacing')).toBeNull();
    expect(screen.getByTestId('start-remember')).toBeInTheDocument();

    await fireEvent.click(screen.getByLabelText('Winkel vergrößern'));
    await fireEvent.click(screen.getByLabelText('Winkel vergrößern'));
    await fireEvent.click(screen.getByLabelText('Winkel vergrößern'));
    await fireEvent.click(screen.getByLabelText('Mehr Randrunden'));
    expect(summary()).toBe('Streifen 45° · 2 Randrunden');

    await fireEvent.click(screen.getByTestId('start-pattern-checkerboard'));
    expect(summary()).toBe('Schachbrett 45° · 2 Randrunden');
  });

  it('hides the angle for edge only and keeps one lap', async () => {
    render(MowSettings, {
      props: {
        offered: ALL,
        spacing: null,
        draft: { name: 'stripes', angle: 0, laps: 0, spacingMm: null },
        remember: true,
      },
    });
    expect(screen.getByTestId('start-angle')).toBeInTheDocument();
    await fireEvent.click(screen.getByTestId('start-pattern-edge_only'));
    expect(screen.queryByTestId('start-angle')).toBeNull();
    expect(summary()).toBe('Nur Rand · 1 Randrunde');
    expect(screen.getByLabelText('Weniger Randrunden')).toHaveProperty('disabled', true);
  });

  it('shows the spacing within the model range and resets it', async () => {
    render(MowSettings, {
      props: { offered: ALL, spacing: [200, 350], draft: { ...FALLBACK }, remember: true },
    });
    const spacing = screen.getByTestId('start-spacing');
    expect(spacing.textContent).toContain('Modell');
    await fireEvent.click(screen.getByLabelText('Weitere Bahnen'));
    await fireEvent.click(screen.getByLabelText('Weitere Bahnen'));
    expect(summary()).toBe('Streifen 0° · 1 Randrunde · 26 cm Bahnabstand');
    await fireEvent.click(screen.getByText('Abstand des Modells verwenden'));
    expect(summary()).toBe('Streifen 0° · 1 Randrunde');
  });

  it('shows the setpoints and jumps to them', async () => {
    const onadjust = vi.fn();
    render(MowSettings, {
      props: {
        offered: ['stripes'],
        spacing: null,
        draft: { ...FALLBACK },
        remember: false,
        blade: '55 mm',
        speed: null,
        onadjust,
      },
    });
    const setpoints = screen.getByTestId('start-setpoints');
    expect(setpoints.textContent).toContain('55 mm');
    expect(setpoints.textContent).toContain('--');
    await fireEvent.click(screen.getByTestId('start-setpoints-adjust'));
    expect(onadjust).toHaveBeenCalledOnce();
  });

  it('offers blade height and speed steppers within the model ranges', async () => {
    const setpoints = { bladeMm: 55, speedMps: 0.4 };
    render(MowSettings, {
      props: {
        offered: ['stripes'],
        spacing: null,
        draft: { ...FALLBACK },
        remember: false,
        setpoints,
        bladeRange: [25, 70] as const,
        speedRange: [0.2, 0.6] as const,
      },
    });
    const blade = screen.getByTestId('start-blade');
    const speed = screen.getByTestId('start-speed');
    expect(blade.querySelector('output')?.dataset.value).toBe('55');
    await fireEvent.click(screen.getByLabelText('Schnitthöhe erhöhen'));
    expect(blade.querySelector('output')?.dataset.value).toBe('60');
    await fireEvent.click(screen.getByLabelText('Langsamer'));
    expect(speed.querySelector('output')?.dataset.value).toBe('0.3');
    expect(screen.queryByTestId('start-setpoints-adjust')).toBeNull();
  });

  it('folds the settings behind "Anpassen" and keeps the summary', async () => {
    setLocale('de', { reload: false });
    render(MowSettings, {
      props: { offered: ALL, spacing: null, draft: { ...FALLBACK }, remember: true },
    });
    const toggle = screen.getByTestId('start-settings-toggle');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.getByTestId('start-settings').classList.contains('expanded')).toBe(false);
    expect(summary()).toBe('Streifen 0° · 1 Randrunde');
    await fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByTestId('start-settings').classList.contains('expanded')).toBe(true);
    expect(toggle).toHaveTextContent('Weniger');
  });

  describe('job options behind "Mehr"', () => {
    const OPTIONS = {
      detection: ['off', 'standard', 'sensitive'],
      obstacleLaps: [0, 4],
      orders: ['border_first', 'grid_first'],
      progress: [0, 99],
    } satisfies JobOptionsSpec;
    const DRAFT = { ...FALLBACK, options: { ...NO_OPTIONS } };

    it('hides "Mehr" when the mower declares no options', () => {
      render(MowSettings, {
        props: { offered: ALL, spacing: null, draft: { ...FALLBACK }, remember: true },
      });
      expect(screen.queryByTestId('start-more')).toBeNull();
    });

    it('offers the declared options with the vendor names and sets them', async () => {
      setLocale('de', { reload: false });
      render(MowSettings, {
        props: {
          offered: ALL,
          spacing: null,
          draft: { ...DRAFT },
          remember: true,
          options: OPTIONS,
        },
      });
      const checked = (id: string) => screen.getByTestId(id).getAttribute('aria-checked');
      const toggle = screen.getByTestId('start-more-toggle');
      expect(toggle).toHaveTextContent('Mehr');
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(screen.queryByTestId('start-obstacle')).toBeNull();
      await fireEvent.click(toggle);

      const modes = screen.getByTestId('start-obstacle').querySelectorAll('[role="radio"]');
      expect([...modes].map((b) => b.textContent?.trim())).toEqual([
        'Aus',
        'Standard',
        'Empfindlich',
      ]);
      expect(screen.getByTestId('start-obstacle').textContent).toContain('Mäher');
      await fireEvent.click(screen.getByTestId('start-obstacle-sensitive'));
      expect(checked('start-obstacle-sensitive')).toBe('true');
      expect(screen.getByTestId('start-obstacle').textContent).toContain('kein Rasen');

      await fireEvent.click(screen.getByTestId('start-order-grid_first'));
      expect(screen.getByTestId('start-order-grid_first')).toHaveTextContent('Zuerst Zickzack');
      expect(checked('start-order-grid_first')).toBe('true');
      // A second tap gives the choice back to the mower.
      await fireEvent.click(screen.getByTestId('start-order-grid_first'));
      expect(checked('start-order-grid_first')).toBe('false');
      expect(checked('start-order-border_first')).toBe('false');

      await fireEvent.click(screen.getByLabelText('Weniger Runden um Sperrzonen'));
      expect(screen.getByTestId('start-nogo-laps').querySelector('output')?.dataset.value).toBe(
        '0',
      );
      await fireEvent.click(screen.getByLabelText('Später beginnen'));
      await fireEvent.click(screen.getByLabelText('Später beginnen'));
      expect(screen.getByTestId('start-progress').querySelector('output')).toHaveTextContent(
        '10 %',
      );
      expect(summary()).toBe('Streifen 0° · 1 Randrunde · ab 10\u00a0%');
      expect(toggle).toHaveTextContent('Mehr · 3');

      // Back to the mower's own setting.
      await fireEvent.click(
        screen.getByTestId('start-obstacle').querySelector('.reset') as HTMLElement,
      );
      expect(checked('start-obstacle-sensitive')).toBe('false');
      expect(toggle).toHaveTextContent('Mehr · 2');
    });

    it('shows only the options the mower declares', async () => {
      render(MowSettings, {
        props: {
          offered: ALL,
          spacing: null,
          draft: { ...DRAFT },
          remember: true,
          options: { detection: [], obstacleLaps: [0, 4], orders: [], progress: null },
        },
      });
      await fireEvent.click(screen.getByTestId('start-more-toggle'));
      expect(screen.getByTestId('start-nogo-laps')).toBeInTheDocument();
      expect(screen.queryByTestId('start-obstacle')).toBeNull();
      expect(screen.queryByTestId('start-order')).toBeNull();
      expect(screen.queryByTestId('start-progress')).toBeNull();
    });
  });
});
