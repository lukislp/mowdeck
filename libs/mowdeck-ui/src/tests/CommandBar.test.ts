import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import CommandBar from '../lib/command/CommandBar.svelte';
import type { CommandAction } from '../lib/types.js';

const start: CommandAction = { id: 'start', label: 'Start', enabled: true, primary: true };
const pause: CommandAction = { id: 'pause', label: 'Pause', enabled: false, reason: 'Not mowing' };

function renderBar(actions: CommandAction[], onaction = vi.fn(), onstop = vi.fn()) {
  render(CommandBar, {
    props: { actions, onaction, onstop, stopLabel: 'Stop', label: 'Commands' },
  });
  return { onaction, onstop };
}

describe('CommandBar', () => {
  it('always renders the emergency stop, even without any action', async () => {
    const { onstop } = renderBar([]);
    const stop = screen.getByTestId('emergency-stop');
    expect(stop).toBeVisible();
    expect(stop).toBeEnabled();
    await fireEvent.click(stop);
    expect(onstop).toHaveBeenCalledOnce();
  });

  it('renders the stop next to every combination of actions', () => {
    for (const actions of [[start], [pause], [start, pause]]) {
      const { unmount } = render(CommandBar, {
        props: { actions, onaction: vi.fn(), onstop: vi.fn(), stopLabel: 'Stop', label: 'C' },
      });
      expect(screen.getByTestId('emergency-stop')).toBeInTheDocument();
      unmount();
    }
  });

  it('sends enabled actions and explains disabled ones', async () => {
    const { onaction } = renderBar([start, pause]);
    await fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    expect(onaction).toHaveBeenCalledWith('start');
    const disabled = screen.getByRole('button', { name: 'Pause' });
    expect(disabled).toBeDisabled();
    expect(disabled).toHaveAttribute('title', 'Not mowing');
  });

  it('does not render an action that was not passed (no permission)', () => {
    renderBar([pause]);
    expect(screen.queryByRole('button', { name: 'Start' })).toBeNull();
  });

  it('blocks a pending action against double taps', () => {
    renderBar([{ ...start, pending: true }]);
    expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled();
  });

  it('shows the stop feedback', () => {
    render(CommandBar, {
      props: {
        actions: [],
        onaction: vi.fn(),
        onstop: vi.fn(),
        stopLabel: 'Stop',
        stopStatus: 'sent',
        stopState: 'sent',
        label: 'C',
      },
    });
    expect(screen.getByRole('status')).toHaveTextContent('sent');
  });
});
