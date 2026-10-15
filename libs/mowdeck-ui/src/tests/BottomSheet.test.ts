import { fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import BottomSheet from '../lib/layout/BottomSheet.svelte';

// jsdom has no ResizeObserver (bind:clientHeight uses it).
globalThis.ResizeObserver ??= class {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
};

const peek = createRawSnippet(() => ({
  render: () => '<div><span>Spirale · 1,5 m</span><button type="button">Start</button></div>',
}));
const children = createRawSnippet(() => ({
  render: () => '<div><button type="button">Muster</button></div>',
}));

function setup(props: Record<string, unknown> = {}) {
  const onsnapchange = vi.fn();
  const view = render(BottomSheet, {
    props: {
      open: true,
      label: 'Spot',
      handleLabel: 'Einstellungen',
      peek,
      children,
      onsnapchange,
      testid: 'sheet',
      ...props,
    },
  });
  return { ...view, onsnapchange };
}

describe('BottomSheet', () => {
  it('opens at peek: the peek content shows, the body is hidden', () => {
    setup();
    expect(screen.getByTestId('sheet')).toHaveAttribute('data-snap', 'peek');
    expect(screen.getByRole('button', { name: 'Start' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Muster' })).toBeNull();
    const handle = screen.getByRole('button', { name: 'Einstellungen' });
    expect(handle).toHaveAttribute('aria-expanded', 'false');
    expect(handle).toHaveAttribute('aria-controls');
  });

  it('a tap on the handle cycles peek, half, full and back', async () => {
    const { onsnapchange } = setup();
    const handle = screen.getByRole('button', { name: 'Einstellungen' });
    await fireEvent.click(handle);
    expect(screen.getByTestId('sheet')).toHaveAttribute('data-snap', 'half');
    expect(handle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('button', { name: 'Muster' })).toBeInTheDocument();
    await fireEvent.click(handle);
    expect(screen.getByTestId('sheet')).toHaveAttribute('data-snap', 'full');
    await fireEvent.click(handle);
    expect(screen.getByTestId('sheet')).toHaveAttribute('data-snap', 'peek');
    expect(onsnapchange.mock.calls.map((c) => c[0])).toEqual(['half', 'full', 'peek']);
  });

  it('Escape goes back to peek', async () => {
    setup({ snap: 'full' });
    expect(screen.getByTestId('sheet')).toHaveAttribute('data-snap', 'full');
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByTestId('sheet')).toHaveAttribute('data-snap', 'peek');
  });

  it('offers only the allowed snaps (a running job keeps it at peek)', async () => {
    setup({ snaps: ['peek'] });
    const handle = screen.getByRole('button', { name: 'Einstellungen' });
    await fireEvent.click(handle);
    expect(screen.getByTestId('sheet')).toHaveAttribute('data-snap', 'peek');
  });

  it('renders nothing when closed', () => {
    setup({ open: false });
    expect(screen.queryByTestId('sheet')).toBeNull();
  });

  it('a pointer drag that does not move is a tap, not a drag', async () => {
    setup();
    const handle = screen.getByRole('button', { name: 'Einstellungen' });
    await fireEvent.pointerDown(handle, { clientY: 500, pointerId: 1 });
    await fireEvent.pointerMove(handle, { clientY: 498, pointerId: 1 });
    await fireEvent.pointerUp(handle, { clientY: 498, pointerId: 1 });
    await fireEvent.click(handle);
    expect(screen.getByTestId('sheet')).toHaveAttribute('data-snap', 'half');
  });
});
