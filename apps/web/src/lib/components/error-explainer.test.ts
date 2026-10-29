import { render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it } from 'vitest';

import {
  explainKey,
  severityLabel,
  severityTone,
  type ErrorExplanation,
} from '#lib/mower/error-explain.js';

import ErrorExplainer from './ErrorExplainer.svelte';

import { setLocale } from '#lib/paraglide/runtime.js';

beforeEach(() => setLocale('de', { reload: false }));

const lifted: ErrorExplanation = {
  code: '2801',
  vendor: 'mammotion',
  vendor_name: 'Mammotion',
  known: true,
  locale: 'de',
  title: 'Angehoben',
  description: 'Der Hebesensor hat Messer und Räder gestoppt.',
  cause: 'Jemand hat den Mäher hochgehoben.',
  steps: ['Sieh sofort nach dem Mäher.', 'Stell ihn auf ebenen Boden.'],
  severity: 'critical',
  category: 'lift_tilt',
  user_action: true,
  links: [],
  verified: true,
};

describe('ErrorExplainer', () => {
  it('lays out a known error: severity, category, vendor code, cause and numbered steps', () => {
    render(ErrorExplainer, { props: { explanation: lifted } });
    const card = screen.getByTestId('error-explainer');
    expect(card.dataset.severity).toBe('critical');
    expect(card.dataset.tone).toBe('error');
    expect(card.getAttribute('lang')).toBe('de');
    expect(screen.getByTestId('error-severity').textContent).toContain('Kritisch');
    expect(screen.getByTestId('error-severity').textContent).toContain('Angehoben oder gekippt');
    expect(screen.getByTestId('error-code').textContent).toContain('Mammotion');
    expect(screen.getByTestId('error-code').textContent).toContain('2801');
    expect(screen.getByTestId('error-title').textContent).toBe('Angehoben');
    expect(screen.getByText('Wahrscheinliche Ursache')).toBeTruthy();
    const steps = screen.getByTestId('error-steps').querySelectorAll('li');
    expect(steps).toHaveLength(2);
    expect(steps[0]!.textContent).toContain('1');
    expect(steps[1]!.textContent).toContain('Stell ihn auf ebenen Boden.');
    expect(screen.queryByTestId('error-unknown')).toBeNull();
  });

  it('marks an unknown code with the mower message and an unverified one', () => {
    const unknown = { ...lifted, known: false, severity: 'warning' as const, title: 'Unbekannt' };
    const { unmount } = render(ErrorExplainer, {
      props: { explanation: unknown, vendorMessage: 'Blade stopped' },
    });
    expect(screen.getByTestId('error-unknown').textContent).toContain('noch keine Erklärung');
    expect(screen.getByText(/Blade stopped/)).toBeTruthy();
    expect(screen.getByTestId('error-explainer').dataset.tone).toBe('paused');
    unmount();
    render(ErrorExplainer, { props: { explanation: { ...lifted, verified: false } } });
    expect(screen.getByTestId('error-unverified').textContent).toContain('nicht vom Hersteller');
  });

  it('says when nothing is to be done and hides the long text when compact', () => {
    render(ErrorExplainer, {
      props: {
        explanation: { ...lifted, severity: 'info', user_action: false },
        compact: true,
      },
    });
    expect(screen.getByText(/Nichts zu tun/)).toBeTruthy();
    expect(screen.queryByText('Wahrscheinliche Ursache')).toBeNull();
    expect(screen.getByTestId('error-explainer').dataset.tone).toBe('unknown');
  });
});

describe('error-explain helpers', () => {
  it('names every severity and maps it to a tone', () => {
    expect(severityLabel('info')).toBe('Hinweis');
    expect(severityLabel('warning')).toBe('Warnung');
    expect(severityLabel('error')).toBe('Störung');
    expect(severityLabel('critical')).toBe('Kritisch');
    expect(severityTone('error')).toBe('error');
  });

  it('keys a lookup by mower, code and language', () => {
    expect(explainKey('m1', '1201', 'de')).toBe('m1|1201|de');
    expect(explainKey('m1', null, 'de')).toBeNull();
    expect(explainKey(undefined, '1201', 'de')).toBeNull();
  });
});
