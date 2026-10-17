import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import LightChip from './LightChip.svelte';

const base = { capable: true, online: true, allowed: true, pending: false };

describe('LightChip', () => {
  it('shows the reported state and asks for the opposite', async () => {
    const onchange = vi.fn();
    render(LightChip, { props: { ...base, on: false, onchange } });
    const chip = screen.getByTestId('light-chip');
    expect(chip.dataset.state).toBe('off');
    expect(chip.getAttribute('role')).toBe('switch');
    expect(chip.getAttribute('aria-checked')).toBe('false');
    expect(chip.getAttribute('aria-describedby')).toBeNull();
    await fireEvent.click(chip);
    expect(onchange).toHaveBeenCalledWith(true);
  });

  it('switches off a light that is on', async () => {
    const onchange = vi.fn();
    render(LightChip, { props: { ...base, on: true, onchange, testid: 'drive-light' } });
    const chip = screen.getByTestId('drive-light');
    expect(chip.dataset.state).toBe('on');
    expect(chip.getAttribute('aria-checked')).toBe('true');
    await fireEvent.click(chip);
    expect(onchange).toHaveBeenCalledWith(false);
  });

  it('waits while a command is pending and says so', async () => {
    const onchange = vi.fn();
    render(LightChip, { props: { ...base, on: false, pending: true, onchange } });
    const chip = screen.getByTestId('light-chip');
    expect(chip.dataset.pending).toBe('true');
    expect(chip.getAttribute('aria-busy')).toBe('true');
    expect(chip).toHaveProperty('disabled', true);
    const note = document.getElementById(chip.getAttribute('aria-describedby')!);
    expect(note?.textContent).toBeTruthy();
    await fireEvent.click(chip);
    expect(onchange).not.toHaveBeenCalled();
  });

  it.each([[{ capable: false }], [{ online: false }], [{ allowed: false }]])(
    'is disabled with a reason when %o',
    (override) => {
      render(LightChip, { props: { ...base, on: null, ...override, onchange: vi.fn() } });
      const chip = screen.getByTestId('light-chip');
      expect(chip).toHaveProperty('disabled', true);
      expect(chip.dataset.state).toBe('unknown');
      expect(chip.getAttribute('aria-describedby')).not.toBeNull();
      expect(chip.title).toBeTruthy();
    },
  );

  it('drops the name when compact', () => {
    const { container } = render(LightChip, {
      props: { ...base, on: false, compact: true, onchange: vi.fn() },
    });
    expect(container.querySelector('.chip.compact')).not.toBeNull();
  });
});
