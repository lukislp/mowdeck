import { fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { setLocale } from '#lib/paraglide/runtime.js';

import type { TourContext, TourOutcome } from './steps';
import { Tour, type TourDeps } from './tour.svelte';
import TourOverlay from './TourOverlay.svelte';

const goto = vi.hoisted(() => vi.fn(async () => undefined));
vi.mock('$app/navigation', () => ({ goto }));
// The app's tour talks to the services; these tests bring their own.
vi.mock('./runtime', () => ({ tour: null }));
// jsdom has no ResizeObserver (the card's size bindings use it).
vi.stubGlobal(
  'ResizeObserver',
  class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  },
);

function context(overrides: Partial<TourContext> = {}): TourContext {
  return {
    mowers: 1,
    mowerId: 'm1',
    capabilities: { manual_drive: true },
    canManage: true,
    modules: new Set(['schedules', 'notifications']),
    demo: false,
    ...overrides,
  };
}

function makeTour(ctx: TourContext = context()): { tour: Tour; saved: TourOutcome[] } {
  const saved: TourOutcome[] = [];
  const deps: TourDeps = {
    store: {
      load: async () => 'new',
      save: async (outcome) => {
        saved.push(outcome);
      },
    },
    context: async () => ctx,
    currentPath: () => '/mowers',
    navigate: async () => undefined,
    findTarget: async (target) => {
      const element = document.createElement('div');
      element.dataset.tour = target;
      document.body.append(element);
      return element;
    },
    resume: { read: () => null, write: () => undefined },
    optedOut: () => false,
    setupStatus: async () => 'new',
    targetTimeoutMs: 1,
  };
  return { tour: new Tour(deps), saved };
}

async function settle(): Promise<void> {
  for (let i = 0; i < 5; i++) {
    await tick();
    await Promise.resolve();
  }
}

describe('TourOverlay', () => {
  beforeEach(() => setLocale('de', { reload: false }));

  it('is a labelled modal dialog with its position, announced politely', async () => {
    const { tour } = makeTour();
    render(TourOverlay, { tour });
    await tour.start();
    await settle();
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName('Willkommen bei MowDeck');
    expect(dialog).toHaveAccessibleDescription(/kurzer Rundgang/);
    expect(screen.getByTestId('tour-progress')).toHaveTextContent('Schritt 1 von 10');
    expect(screen.getByTestId('tour-live')).toHaveAttribute('aria-live', 'polite');
    expect(screen.getByTestId('tour-live')).toHaveTextContent(
      'Schritt 1 von 10: Willkommen bei MowDeck',
    );
    // The first step has no Back.
    expect(screen.queryByTestId('tour-back')).toBeNull();
    expect(document.activeElement).toBe(screen.getByTestId('tour-next'));
  });

  it('steps with the arrow keys and leaves with Esc', async () => {
    const { tour, saved } = makeTour();
    render(TourOverlay, { tour });
    await tour.start();
    await settle();
    await fireEvent.keyDown(window, { key: 'ArrowRight' });
    await settle();
    expect(screen.getByTestId('tour')).toHaveAttribute('data-step', 'overview');
    expect(screen.getByTestId('tour-frame')).toBeInTheDocument();
    await fireEvent.keyDown(window, { key: 'ArrowLeft' });
    await settle();
    expect(screen.getByTestId('tour')).toHaveAttribute('data-step', 'welcome');
    await fireEvent.keyDown(window, { key: 'Escape' });
    await settle();
    expect(screen.queryByTestId('tour')).toBeNull();
    expect(saved).toEqual(['seen', 'skipped']);
  });

  it('keeps the focus inside the card', async () => {
    const outside = document.createElement('button');
    document.body.append(outside);
    const { tour } = makeTour();
    render(TourOverlay, { tour });
    await tour.start();
    await tour.next();
    await settle();
    const back = screen.getByTestId('tour-back');
    const leave = screen.getByTestId('tour-skip');
    const next = screen.getByTestId('tour-next');
    next.focus();
    await fireEvent.keyDown(window, { key: 'Tab' });
    expect(document.activeElement).toBe(leave);
    await fireEvent.keyDown(window, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(next);
    expect(back).toBeInTheDocument();
    outside.focus();
    await fireEvent.focusIn(outside);
    expect(document.activeElement).toBe(next);
  });

  it('explains what the demo locks, in German and English', async () => {
    const { tour } = makeTour(context({ demo: true }));
    render(TourOverlay, { tour });
    await tour.start({ at: 'settings' });
    await settle();
    expect(screen.getByTestId('tour-demo-note')).toHaveTextContent(/in der Demo gesperrt/);
    tour.skip();
    setLocale('en', { reload: false });
    await tour.start({ at: 'settings' });
    await settle();
    expect(screen.getByTestId('tour-demo-note')).toHaveTextContent(/locked in the demo/);
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Settings');
  });

  it('shows no demo note outside the demo', async () => {
    const { tour } = makeTour();
    render(TourOverlay, { tour });
    await tour.start({ at: 'settings' });
    await settle();
    expect(screen.queryByTestId('tour-demo-note')).toBeNull();
  });

  it('ends a tour without a mower with "Add mower", into the add flow', async () => {
    const { tour, saved } = makeTour(context({ mowers: 0, mowerId: null }));
    render(TourOverlay, { tour });
    await tour.start({ at: 'add-mower' });
    await settle();
    const add = screen.getByTestId('tour-next');
    expect(add).toHaveTextContent('Mäher hinzufügen');
    await fireEvent.click(add);
    await settle();
    expect(saved.at(-1)).toBe('done');
    expect(goto).toHaveBeenCalledWith('/mowers/new');
  });

  it('says "Done" on the last step', async () => {
    const { tour, saved } = makeTour();
    render(TourOverlay, { tour });
    await tour.start({ at: 'restart' });
    await settle();
    const done = screen.getByTestId('tour-next');
    expect(done).toHaveTextContent('Fertig');
    await fireEvent.click(done);
    await settle();
    expect(screen.queryByTestId('tour')).toBeNull();
    expect(saved.at(-1)).toBe('done');
  });
});
