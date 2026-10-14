import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import LightToggle from './LightToggle.svelte';

const base = { capable: true, online: true, allowed: true, pending: false };

describe('LightToggle', () => {
  it('shows the reported state and asks for the opposite', async () => {
    const onchange = vi.fn();
    render(LightToggle, { props: { ...base, on: false, onchange } });
    const toggle = screen.getByTestId('light-toggle');
    expect(screen.getByTestId('light-control').dataset.state).toBe('off');
    expect(toggle.getAttribute('aria-checked')).toBe('false');
    await fireEvent.click(toggle);
    expect(onchange).toHaveBeenCalledWith(true);
  });

  it('switches off a light that is on', async () => {
    const onchange = vi.fn();
    render(LightToggle, { props: { ...base, on: true, onchange } });
    expect(screen.getByTestId('light-toggle').getAttribute('aria-checked')).toBe('true');
    await fireEvent.click(screen.getByTestId('light-toggle'));
    expect(onchange).toHaveBeenCalledWith(false);
  });

  it('waits while a command is pending', () => {
    render(LightToggle, { props: { ...base, on: false, pending: true, onchange: vi.fn() } });
    expect(screen.getByTestId('light-control').dataset.pending).toBe('true');
    expect(screen.getByTestId('light-toggle')).toHaveProperty('disabled', true);
  });

  it.each([
    [{ capable: false }, 'unsupported'],
    [{ online: false }, 'offline'],
    [{ allowed: false }, 'no permission'],
  ])('is disabled when %o', (override, _why) => {
    render(LightToggle, { props: { ...base, on: null, ...override, onchange: vi.fn() } });
    expect(screen.getByTestId('light-toggle')).toHaveProperty('disabled', true);
    expect(screen.getByTestId('light-control').dataset.state).toBe('unknown');
  });
});
