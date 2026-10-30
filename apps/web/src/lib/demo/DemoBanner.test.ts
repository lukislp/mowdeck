import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import { demoTitle, keepDemoTitle } from './demo';
import DemoBanner from './DemoBanner.svelte';

describe('DemoBanner', () => {
  it('names the demo and its reset interval and cannot be dismissed', () => {
    render(DemoBanner, { props: { resetHours: 4, href: 'https://example.org/src' } });
    const band = screen.getByTestId('demo-banner');
    expect(band).toHaveTextContent(/demo/i);
    expect(band).toHaveTextContent(/4 h/);
    expect(band.querySelector('button')).toBeNull();
    expect(screen.getByRole('link')).toHaveAttribute('href', 'https://example.org/src');
    expect(screen.queryByTestId('demo-api-explorer')).toBeNull();
  });

  it('links the API explorer when the demo has it', () => {
    render(DemoBanner, {
      props: { resetHours: 6, href: 'https://example.org/src', explorer: true },
    });
    expect(screen.getByTestId('demo-api-explorer')).toHaveAttribute('href', '/api-explorer');
    expect(screen.getAllByRole('link')).toHaveLength(2);
  });
});

describe('keepDemoTitle', () => {
  it('keeps the label in front while pages change the title', async () => {
    document.title = 'Mowers · MowDeck';
    const stop = keepDemoTitle(document, 'Demo');
    expect(document.title).toBe('Demo · Mowers · MowDeck');
    document.title = 'Settings · MowDeck';
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.title).toBe(demoTitle('Settings · MowDeck', 'Demo'));
    stop();
    document.title = 'Plain';
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.title).toBe('Plain');
  });
});
