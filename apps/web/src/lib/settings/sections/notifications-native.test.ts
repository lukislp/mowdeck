// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The settings entry "Change server" inside the app (ADR 0068, ADR 0036): in the public demo the
 * channels stay locked (the demo sends nothing outside the app), but leaving the server is a
 * local action of the app and must stay usable.
 */
import { render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { demo } from '#lib/demo/state.svelte.js';
import { setLocale } from '#lib/paraglide/runtime.js';

const { nativeShell, prefs } = vi.hoisted(() => ({
  nativeShell: {
    active: true,
    server: 'https://demo.example.org' as string | null,
    platform: 'ios',
    apns: 'idle',
    apnsAvailable: false,
    widgetsAvailable: false,
    watchAvailable: false,
    watch: 'unsupported',
    widgets: 'unsupported',
    local: null,
    ntfy: false,
    changeServer: vi.fn(async () => {}),
    refreshWatch: vi.fn(async () => {}),
    refreshNtfy: vi.fn(async () => {}),
  },
  prefs: {
    stored: true,
    available_kinds: [],
    enabled_kinds: [],
    all_mowers: true,
    mower_ids: [],
    quiet_hours: null,
    channels: ['inbox'],
    available_channels: ['inbox'],
    languages: ['de', 'en'],
    language: 'de',
  },
}));

vi.mock('#lib/native/runtime.js', () => ({ nativeShell }));
vi.mock('#lib/api/client.js', () => ({
  notificationsApi: { GET: vi.fn(async () => ({ data: prefs })) },
  fleetApi: { GET: vi.fn(async () => ({ data: { items: [] } })) },
  unwrap: vi.fn(async (result: Promise<{ data: unknown }>) => (await result).data),
}));
vi.mock('#lib/notifications/runtime.js', () => ({
  inbox: { refresh: vi.fn() },
  pushDevice: { deviceId: () => null, subscribed: vi.fn(), enable: vi.fn(), remove: vi.fn() },
}));

import NotificationsSection from './NotificationsSection.svelte';

beforeEach(() => {
  setLocale('de', { reload: false });
  nativeShell.active = true;
});

afterEach(() => {
  demo.config = null;
});

describe('NotificationsSection in the app', () => {
  it('keeps "Change server" usable in the demo while the channels stay locked', async () => {
    demo.config = { resetHours: 6 };
    render(NotificationsSection);
    const entry = await screen.findByTestId('native-change-server');
    expect(entry).toBeEnabled();
    expect(entry.closest('fieldset')).toBeNull();
    expect(screen.getByTestId('demo-lock-channels').querySelector('fieldset')).toBeDisabled();
    expect(screen.getByTestId('native-panel')).toBeInTheDocument();
  });

  it('shows the same entry outside the demo, and none in a browser', async () => {
    const app = render(NotificationsSection);
    expect(await screen.findByTestId('native-change-server')).toBeEnabled();
    app.unmount();

    nativeShell.active = false;
    render(NotificationsSection);
    await screen.findByTestId('notif-preferences');
    expect(screen.queryByTestId('native-change-server')).toBeNull();
  });
});
