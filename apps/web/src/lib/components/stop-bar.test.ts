import { fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { describe, expect, it, vi } from 'vitest';

const sendCommand = vi.fn(async () => true);

vi.mock('#lib/live.svelte.js', () => ({
  commands: { latest: () => undefined },
  sendCommand: (...args: unknown[]) => sendCommand(...(args as [])),
}));

// jsdom has no ResizeObserver (`bind:clientHeight` uses one).
globalThis.ResizeObserver ??= class {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
} as unknown as typeof ResizeObserver;
window.matchMedia ??= ((query: string) => ({
  matches: false,
  media: query,
  addEventListener() {},
  removeEventListener() {},
})) as unknown as typeof window.matchMedia;

const { default: StopBar } = await import('./StopBar.svelte');

const sessionStop = createRawSnippet(() => ({
  render: () => '<button type="button" data-testid="extra">Stopp</button>',
}));

describe('StopBar', () => {
  it('is the bottom bar by default and leaves the extra controls out', () => {
    render(StopBar, { props: { mowerId: 'm1', children: sessionStop } });
    expect(screen.getByTestId('stop-bar').dataset.variant).toBe('bar');
    expect(screen.getByTestId('emergency-stop')).not.toHaveClass('upright');
    expect(screen.queryByTestId('extra')).toBeNull();
    // On a phone the toasts are lifted above the bar.
    expect(document.documentElement.style.getPropertyValue('--md-toast-offset')).not.toBe('16px');
    // A bar takes the bottom edge, never the side.
    expect(document.documentElement.style.getPropertyValue('--md-stop-inset-start')).toBe('');
  });

  it('withdraws the published clearance when it leaves the page', () => {
    const root = document.documentElement;
    root.style.setProperty('--md-stop-inset-start', '112px');
    const { unmount } = render(StopBar, { props: { mowerId: 'm1' } });
    expect(root.style.getPropertyValue('--md-stop-inset-start')).toBe('');
    root.style.setProperty('--md-stop-clearance', '120px');
    unmount();
    expect(root.style.getPropertyValue('--md-stop-clearance')).toBe('');
    expect(root.style.getPropertyValue('--md-toast-offset')).toBe('');
  });

  it('has a rail form with the stop upright and the extra controls above it', async () => {
    const before = vi.fn();
    render(StopBar, {
      props: { mowerId: 'm1', rail: true, onbeforestop: before, children: sessionStop },
    });
    const bar = screen.getByTestId('stop-bar');
    expect(bar.dataset.variant).toBe('rail');
    const stop = screen.getByTestId('emergency-stop');
    expect(stop).toHaveClass('upright');
    const extra = screen.getByTestId('extra');
    // The extra controls come first, the stop at the foot.
    expect(extra.compareDocumentPosition(stop) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await fireEvent.click(stop);
    expect(before).toHaveBeenCalledOnce();
    expect(sendCommand).toHaveBeenCalledWith('m1', { kind: 'emergency_stop' });
    // The rail is at the side: the toasts keep their place at the bottom.
    expect(document.documentElement.style.getPropertyValue('--md-toast-offset')).toBe('16px');
    expect(document.documentElement.style.getPropertyValue('--md-stop-clearance')).toBe('');
  });
});
