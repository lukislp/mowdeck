import { fireEvent, render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { setLocale } from '#lib/paraglide/runtime.js';

import MaintenancePanel from './MaintenancePanel.svelte';

beforeEach(() => setLocale('de', { reload: false }));

const ACTIONS = ['leave_dock', 'sync_map', 'restart_mower'] as const;

describe('MaintenancePanel', () => {
  it('lists the actions and disables a blocked one with its reason', () => {
    render(MaintenancePanel, {
      props: {
        actions: [...ACTIONS],
        blockers: { leave_dock: null, sync_map: null, restart_mower: 'not_at_rest' },
        results: {},
        driveSession: false,
        onrun: vi.fn(),
      },
    });
    expect(screen.getByTestId('maint-sync_map').textContent).toContain('Karte neu laden');
    expect(screen.getByTestId('maint-restart_mower-open')).toHaveProperty('disabled', true);
    expect(screen.getByTestId('maint-restart_mower-reason').textContent).toContain(
      'beende erst den Auftrag',
    );
    expect(screen.getByTestId('maint-sync_map-open')).toHaveProperty('disabled', false);
  });

  it('asks first: cancel sends nothing, run sends the action', async () => {
    const onrun = vi.fn();
    render(MaintenancePanel, {
      props: {
        actions: [...ACTIONS],
        blockers: {},
        results: {},
        driveSession: true,
        onrun,
      },
    });
    await fireEvent.click(screen.getByTestId('maint-leave_dock-open'));
    expect(screen.getByTestId('maint-confirm-body').textContent).toContain('Ladestation');
    expect(screen.getByTestId('maint-confirm-session').textContent).toContain('manuelle Fahrt');
    await fireEvent.click(screen.getByRole('button', { name: 'Abbrechen' }));
    expect(onrun).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByTestId('maint-restart_mower-open'));
    expect(screen.getByTestId('maint-confirm-body').textContent).toContain('startet neu');
    expect(screen.queryByTestId('maint-confirm-session')).toBeNull();
    await fireEvent.click(screen.getByTestId('maint-confirm-go'));
    expect(onrun).toHaveBeenCalledWith('restart_mower');
  });

  it('shows the last result in words', () => {
    render(MaintenancePanel, {
      props: {
        actions: [...ACTIONS],
        blockers: {},
        results: {
          restart_mower: { status: 'accepted', reason: null },
          sync_map: { status: 'executed', reason: 'map read again: 2 areas' },
          leave_dock: { status: 'rejected', reason: 'the mower is not in its dock' },
        },
        driveSession: false,
        onrun: vi.fn(),
      },
    });
    expect(screen.getByTestId('maint-restart_mower-result').textContent).toContain(
      'an den Mäher übergeben',
    );
    expect(screen.getByTestId('maint-sync_map-result').dataset.tone).toBe('ok');
    expect(screen.getByTestId('maint-sync_map-result').textContent).toContain('2 areas');
    expect(screen.getByTestId('maint-leave_dock-result').dataset.tone).toBe('error');
  });
});
