import { fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import Stepper from '../lib/form/Stepper.svelte';

function renderStepper(value: number | null, oncommit = vi.fn()) {
  render(Stepper, {
    props: {
      id: 'blade',
      label: 'Blade height',
      value,
      min: 20,
      max: 100,
      step: 5,
      unit: 'mm',
      decreaseLabel: 'Lower',
      increaseLabel: 'Raise',
      oncommit,
    },
  });
  return oncommit;
}

describe('Stepper', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('commits once, 600 ms after the last step', async () => {
    const oncommit = renderStepper(40);
    const raise = screen.getByRole('button', { name: 'Raise' });
    await fireEvent.click(raise);
    await fireEvent.click(raise);
    expect(screen.getByTestId('blade-value')).toHaveTextContent('50mm');
    vi.advanceTimersByTime(599);
    expect(oncommit).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(oncommit).toHaveBeenCalledExactlyOnceWith(50);
  });

  it('keeps the value within the capability range', async () => {
    const oncommit = renderStepper(100);
    expect(screen.getByRole('button', { name: 'Raise' })).toBeDisabled();
    await fireEvent.click(screen.getByRole('button', { name: 'Lower' }));
    vi.advanceTimersByTime(600);
    expect(oncommit).toHaveBeenCalledWith(95);
  });

  it('does not commit when the steps return to the confirmed value', async () => {
    const oncommit = renderStepper(40);
    await fireEvent.click(screen.getByRole('button', { name: 'Raise' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Lower' }));
    vi.advanceTimersByTime(600);
    expect(oncommit).not.toHaveBeenCalled();
  });

  it('commits at every step with immediate', async () => {
    const oncommit = vi.fn();
    render(Stepper, {
      props: {
        id: 'speed',
        label: 'Speed',
        value: 0.3,
        min: 0.2,
        max: 0.6,
        step: 0.1,
        unit: 'm/s',
        decimals: 2,
        decreaseLabel: 'Slower',
        increaseLabel: 'Faster',
        immediate: true,
        oncommit,
      },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Faster' }));
    expect(oncommit).toHaveBeenCalledExactlyOnceWith(0.4);
  });
});
