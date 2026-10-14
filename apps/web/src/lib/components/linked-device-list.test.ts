import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import type { LinkedDevice } from '$lib/linked/accounts';
import { m } from '$lib/paraglide/messages';

import LinkedDeviceList from './LinkedDeviceList.svelte';

function device(name: string, extra: Partial<LinkedDevice> = {}): LinkedDevice {
  return {
    device_name: name,
    model: null,
    iot_id: null,
    product_key: null,
    online: null,
    is_mower: true,
    registered: false,
    mower_id: null,
    registered_elsewhere: false,
    ...extra,
  };
}

const DEVICES = [
  device('RTK-FAKE0001', { is_mower: false, online: true }),
  device('Luba-FAKE0002', { online: false, iot_id: 'fakeiot0002' }),
  device('Luba-FAKE0001', { online: true, iot_id: 'fakeiot0001', model: 'LUBA 2 AWD 5000X' }),
  device('Luba-ADDED', { registered: true }),
  device('Luba-FOREIGN', { registered_elsewhere: true }),
];

describe('LinkedDeviceList', () => {
  it('offers checkboxes only for addable mowers and greys out the rest with the reason', () => {
    render(LinkedDeviceList, { props: { devices: DEVICES, lastSyncedAt: null } });
    expect(screen.getByTestId('linked-device-Luba-FAKE0001').dataset.state).toBe('addable');
    expect(screen.getByTestId('linked-device-check-Luba-FAKE0001')).toBeInTheDocument();
    expect(screen.getByTestId('linked-device-check-Luba-FAKE0002')).toBeInTheDocument();
    for (const [name, state, reason] of [
      ['RTK-FAKE0001', 'not_mower', m.linked_device_not_mower()],
      ['Luba-ADDED', 'registered', m.linked_device_registered()],
      ['Luba-FOREIGN', 'elsewhere', m.linked_device_elsewhere()],
    ] as const) {
      expect(screen.getByTestId(`linked-device-${name}`).dataset.state).toBe(state);
      expect(screen.queryByTestId(`linked-device-check-${name}`)).toBeNull();
      expect(screen.getByTestId(`linked-device-reason-${name}`)).toHaveTextContent(reason);
    }
    expect(screen.getByText('LUBA 2 AWD 5000X')).toBeInTheDocument();
    expect(screen.getByText(/fakeiot0001/)).toBeInTheDocument();
    expect(screen.getByTestId('linked-last-sync')).toHaveTextContent(m.linked_never_synced());
    // Addable first: the list order follows sortDevices.
    const order = screen
      .getAllByTestId(/^linked-device-[A-Za-z]+-[A-Z0-9]+$/)
      .map((row) => row.dataset.testid);
    expect(order[0]).toBe('linked-device-Luba-FAKE0001');
  });

  it('shows online, offline and unknown with a word, not colour alone', () => {
    render(LinkedDeviceList, { props: { devices: DEVICES, lastSyncedAt: null } });
    const online = screen.getByTestId('linked-device-online-Luba-FAKE0001');
    expect(online.dataset.value).toBe('online');
    expect(online).toHaveTextContent(m.linked_device_online());
    expect(screen.getByTestId('linked-device-online-Luba-FAKE0002').dataset.value).toBe('offline');
    expect(screen.getByTestId('linked-device-online-Luba-ADDED').dataset.value).toBe('unknown');
  });

  it('selects with the checkbox (label included) and counts the selection', async () => {
    render(LinkedDeviceList, { props: { devices: DEVICES, lastSyncedAt: null } });
    const box = screen.getByLabelText('Luba-FAKE0001');
    await fireEvent.click(box);
    expect(box).toBeChecked();
    expect(screen.getByTestId('linked-selected-count')).toHaveTextContent(
      m.linked_selected_count({ count: 1 }),
    );
    await fireEvent.click(screen.getByTestId('linked-device-check-Luba-FAKE0002'));
    expect(screen.getByTestId('linked-selected-count')).toHaveTextContent(
      m.linked_selected_count({ count: 2 }),
    );
    await fireEvent.click(box);
    expect(box).not.toBeChecked();
  });

  it('re-reads on demand and shows failures of the last attempt', async () => {
    const onsync = vi.fn();
    render(LinkedDeviceList, {
      props: {
        devices: DEVICES,
        selected: ['Luba-FAKE0002'],
        lastSyncedAt: '2026-10-11T08:00:00Z',
        onsync,
        failures: { 'Luba-FAKE0002': 'Already registered' },
      },
    });
    expect(screen.getByTestId('linked-device-check-Luba-FAKE0002')).toBeChecked();
    expect(screen.getByTestId('linked-device-failure-Luba-FAKE0002')).toHaveTextContent(
      'Already registered',
    );
    expect(screen.getByTestId('linked-last-sync')).not.toHaveTextContent(m.linked_never_synced());
    await fireEvent.click(screen.getByTestId('linked-resync'));
    expect(onsync).toHaveBeenCalledOnce();
  });

  it('says so when the account has no devices', () => {
    render(LinkedDeviceList, { props: { devices: [], lastSyncedAt: null, syncing: true } });
    expect(screen.getByTestId('linked-devices-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('linked-resync')).toBeNull();
  });
});
