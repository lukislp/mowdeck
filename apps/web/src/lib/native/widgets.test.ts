import { describe, expect, it, vi } from 'vitest';

import type { NativePlugin } from './capacitor';
import { WidgetsRegistration, wholeSeconds, type WidgetsApi } from './widgets';

function fakeShell(status: Record<string, unknown> = { supported: true, configured: false }) {
  const calls: { method: string; options?: object }[] = [];
  const listeners = new Map<string, (data: unknown) => void>();
  let pending: unknown[] = [];
  const plugin: NativePlugin = {
    async call(method, options) {
      calls.push({ method, options });
      if (method === 'getWidgetStatus') return status as never;
      if (method === 'getInstallationId') return { id: 'inst-1' } as never;
      if (method === 'getPendingActivityRegistrations') return { items: pending } as never;
      return undefined as never;
    },
    on(event, callback) {
      listeners.set(event, callback);
      return () => listeners.delete(event);
    },
  };
  return {
    plugin,
    calls,
    emit: (event: string, data: unknown) => listeners.get(event)?.(data),
    setPending: (items: unknown[]) => (pending = items),
  };
}

function fakeApi(overrides: Partial<WidgetsApi> = {}): WidgetsApi {
  return {
    createCredential: vi.fn(async () => ({
      id: 'cred-1',
      token: 'mdk_secret',
      expires_at: '2027-01-25T12:00:00.123456Z',
    })),
    revokeCredential: vi.fn(async () => undefined),
    mowers: vi.fn(async () => [{ id: 'm1', name: 'Luba' }]),
    registerDevice: vi.fn(async () => undefined),
    unregisterDevice: vi.fn(async () => undefined),
    reportActivity: vi.fn(async () => undefined),
    endActivity: vi.fn(async () => undefined),
    ...overrides,
  };
}

function registration(shell: ReturnType<typeof fakeShell>, api: WidgetsApi) {
  return new WidgetsRegistration({
    shell: shell.plugin,
    api,
    serverUrl: () => 'https://mow.example',
    deviceName: 'MowDeck iOS',
  });
}

describe('wholeSeconds', () => {
  it('drops fractional seconds, which the Swift ISO8601DateFormatter does not parse', () => {
    expect(wholeSeconds('2027-01-25T12:00:00.123456Z')).toBe('2027-01-25T12:00:00Z');
    expect(wholeSeconds('2027-01-25T12:00:00Z')).toBe('2027-01-25T12:00:00Z');
  });
});

describe('WidgetsRegistration', () => {
  it('enable creates a credential, hands it to the shell and registers the device', async () => {
    const shell = fakeShell({ supported: true, configured: false, pushToStartToken: 'ab12' });
    const api = fakeApi();
    const widgets = registration(shell, api);
    expect(await widgets.enable()).toBe('on');
    expect(api.createCredential).toHaveBeenCalledWith('MowDeck iOS', null);
    const configure = shell.calls.find((c) => c.method === 'configureWidgets');
    expect(configure?.options).toEqual({
      serverUrl: 'https://mow.example',
      secret: 'mdk_secret',
      credentialId: 'cred-1',
      expiresAt: '2027-01-25T12:00:00Z',
      deviceName: 'MowDeck iOS',
      mowers: [{ id: 'm1', name: 'Luba' }],
    });
    expect(api.registerDevice).toHaveBeenCalledWith({
      installation_id: 'inst-1',
      push_to_start_token: 'ab12',
      widgets_enabled: true,
    });
  });

  it('enable replaces the credential this device already holds', async () => {
    const shell = fakeShell({ supported: true, configured: true, credentialId: 'old' });
    const api = fakeApi();
    await registration(shell, api).enable();
    expect(api.createCredential).toHaveBeenCalledWith('MowDeck iOS', 'old');
  });

  it('enable ends in error when the server refuses, and leaves the shell untouched', async () => {
    const shell = fakeShell();
    const api = fakeApi({
      createCredential: vi.fn(async () => {
        throw new Error('403');
      }),
    });
    const widgets = registration(shell, api);
    expect(await widgets.enable()).toBe('error');
    expect(shell.calls.some((c) => c.method === 'configureWidgets')).toBe(false);
  });

  it('disable forgets the device, revokes the credential and clears the shell', async () => {
    const shell = fakeShell({ supported: true, configured: true, credentialId: 'cred-9' });
    const api = fakeApi();
    const widgets = registration(shell, api);
    await widgets.disable();
    expect(api.unregisterDevice).toHaveBeenCalledWith('inst-1');
    expect(api.revokeCredential).toHaveBeenCalledWith('cred-9');
    expect(shell.calls.some((c) => c.method === 'clearWidgets')).toBe(true);
    expect(widgets.state).toBe('off');
  });

  it('maintain does nothing when widgets are off', async () => {
    const shell = fakeShell({ supported: true, configured: false });
    const api = fakeApi();
    const widgets = registration(shell, api);
    await widgets.maintain();
    expect(api.createCredential).not.toHaveBeenCalled();
    expect(api.registerDevice).not.toHaveBeenCalled();
    expect(widgets.state).toBe('off');
  });

  it('maintain rotates a credential that expires soon', async () => {
    const shell = fakeShell({
      supported: true,
      configured: true,
      credentialId: 'old',
      needsRotation: true,
    });
    const api = fakeApi();
    await registration(shell, api).maintain();
    expect(api.createCredential).toHaveBeenCalledWith('MowDeck iOS', 'old');
    expect(shell.calls.some((c) => c.method === 'configureWidgets')).toBe(true);
  });

  it('maintain refreshes the mowers and hands over tokens collected without the web app', async () => {
    const shell = fakeShell({ supported: true, configured: true, credentialId: 'c' });
    shell.setPending([
      { mowerId: 'm1', installationId: 'inst-1', activityId: 'ABC', pushToken: 'ff00' },
    ]);
    const api = fakeApi();
    await registration(shell, api).maintain();
    expect(shell.calls.find((c) => c.method === 'setWidgetMowers')?.options).toEqual({
      mowers: [{ id: 'm1', name: 'Luba' }],
    });
    expect(api.reportActivity).toHaveBeenCalledWith('m1', {
      installation_id: 'inst-1',
      activity_id: 'ABC',
      push_token: 'ff00',
    });
    expect(
      shell.calls.find((c) => c.method === 'clearPendingActivityRegistration')?.options,
    ).toEqual({ mowerId: 'm1' });
  });

  it('keeps a token pending when the server is unreachable', async () => {
    const shell = fakeShell({ supported: true, configured: true });
    shell.setPending([
      { mowerId: 'm1', installationId: 'inst-1', activityId: 'ABC', pushToken: 'ff00' },
    ]);
    const api = fakeApi({
      reportActivity: vi.fn(async () => {
        throw new Error('offline');
      }),
    });
    await registration(shell, api).maintain();
    expect(shell.calls.some((c) => c.method === 'clearPendingActivityRegistration')).toBe(false);
  });

  it('answers the shell events: push-to-start token, activity token, ended activity', async () => {
    const shell = fakeShell({ supported: true, configured: true, pushToStartToken: 'aa' });
    const api = fakeApi();
    const widgets = registration(shell, api);
    await widgets.maintain();
    vi.mocked(api.registerDevice).mockClear();
    shell.emit('pushToStartToken', { token: 'bb' });
    shell.emit('liveActivityToken', {
      mowerId: 'm1',
      installationId: 'inst-1',
      activityId: 'X',
      pushToken: 'cc',
    });
    shell.emit('liveActivityEnded', { mowerId: 'm1', activityId: 'X' });
    await vi.waitFor(() => expect(api.endActivity).toHaveBeenCalledWith('m1', 'inst-1'));
    expect(api.registerDevice).toHaveBeenCalled();
    expect(api.reportActivity).toHaveBeenCalledWith('m1', {
      installation_id: 'inst-1',
      activity_id: 'X',
      push_token: 'cc',
    });
    widgets.stop();
  });

  it('is unsupported when the shell has no widget methods', async () => {
    const shell = fakeShell();
    shell.plugin.call = async () => {
      throw new Error('not implemented');
    };
    const widgets = registration(shell, fakeApi());
    expect(await widgets.refresh()).toBe('unsupported');
  });
});
