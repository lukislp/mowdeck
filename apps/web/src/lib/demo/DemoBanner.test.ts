import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

const { session, navigatePath } = vi.hoisted(() => ({
  session: { status: 'anonymous' as string, tryDemo: vi.fn(async () => {}) },
  navigatePath: vi.fn(async () => {}),
}));
vi.mock('#lib/auth/session.svelte.js', () => ({ session }));
vi.mock('#lib/navigate.js', () => ({ navigatePath }));

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

  it('signs in to the demo first when the explorer is opened signed out', async () => {
    session.status = 'anonymous';
    session.tryDemo.mockClear();
    navigatePath.mockClear();
    render(DemoBanner, {
      props: { resetHours: 6, href: 'https://example.org/src', explorer: true },
    });
    await fireEvent.click(screen.getByTestId('demo-api-explorer'));
    await waitFor(() => expect(navigatePath).toHaveBeenCalledWith('/api-explorer'));
    expect(session.tryDemo).toHaveBeenCalledOnce();
  });

  it('just follows the link when already signed in', async () => {
    session.status = 'authenticated';
    session.tryDemo.mockClear();
    navigatePath.mockClear();
    render(DemoBanner, {
      props: { resetHours: 6, href: 'https://example.org/src', explorer: true },
    });
    await fireEvent.click(screen.getByTestId('demo-api-explorer'));
    expect(session.tryDemo).not.toHaveBeenCalled();
    expect(navigatePath).not.toHaveBeenCalled();
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
