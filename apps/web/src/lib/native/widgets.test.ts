import { describe, expect, it, vi } from 'vitest';

import type { NativePlugin } from './capacitor';
import {
  CREDENTIAL_KEY,
  REVOKE_KEY,
  WANTED_KEY,
  WidgetsRegistration,
  wholeSeconds,
  type WidgetsApi,
} from './widgets';

function fakeShell(status: Record<string, unknown> = { supported: true, configured: false }) {
  const calls: { method: string; options?: object }[] = [];
  const listeners = new Map<string, (data: unknown) => void>();
  let pending: unknown[] = [];
  const plugin: NativePlugin = {
    async call(method, options) {
      calls.push({ method, options });
      if (method === 'getWidgetStatus') return status as never;
      // like the shell: clearing removes the Keychain item, configuring stores one
      if (method === 'clearWidgets') status = { ...status, configured: false, credentialId: null };
      if (method === 'configureWidgets') {
        const id = (options as { credentialId?: string } | undefined)?.credentialId ?? null;
        status = { ...status, configured: true, credentialId: id };
      }
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

function memoryStorage(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
    removeItem: (key: string) => void data.delete(key),
  };
}

function problem(status: number): Error & { status: number } {
  return Object.assign(new Error(String(status)), { status });
}

function remembering(
  shell: ReturnType<typeof fakeShell>,
  api: WidgetsApi,
  storage: ReturnType<typeof memoryStorage>,
  user: string | null = 'lukas@home',
  session: string | null = null,
) {
  return new WidgetsRegistration({
    shell: shell.plugin,
    api,
    serverUrl: () => 'https://mow.example',
    deviceName: 'MowDeck iOS',
    storage,
    user: () => user,
    session: () => session,
    retryMs: 5,
  });
}

const held = (storage: ReturnType<typeof memoryStorage>) =>
  JSON.parse(storage.getItem(CREDENTIAL_KEY) ?? 'null') as { id: string; user: string } | null;

describe('WidgetsRegistration across sign-out and sign-in', () => {
  it('sign-out ends the credential and clears the widgets, but keeps the wish', async () => {
    const storage = memoryStorage({ [WANTED_KEY]: '1' });
    const shell = fakeShell({ supported: true, configured: true, credentialId: 'cred-1' });
    const api = fakeApi();
    const widgets = remembering(shell, api, storage);
    await widgets.signOut();
    expect(api.unregisterDevice).toHaveBeenCalledWith('inst-1');
    expect(api.revokeCredential).toHaveBeenCalledWith('cred-1');
    expect(shell.calls.find((c) => c.method === 'clearWidgets')?.options).toEqual({
      signedOut: true,
    });
    expect(storage.getItem(WANTED_KEY)).toBe('1');
    expect(storage.getItem(CREDENTIAL_KEY)).toBeNull();
    expect(widgets.state).not.toBe('off');
  });

  it('the next sign-in sets the widgets up again without the switch', async () => {
    const storage = memoryStorage({ [WANTED_KEY]: '1' });
    const shell = fakeShell({ supported: true, configured: false });
    const api = fakeApi();
    const widgets = remembering(shell, api, storage);
    await widgets.maintain();
    expect(api.createCredential).toHaveBeenCalledWith('MowDeck iOS', null);
    expect(shell.calls.some((c) => c.method === 'configureWidgets')).toBe(true);
    expect(widgets.state).toBe('on');
    expect(held(storage)).toMatchObject({ id: 'cred-1', user: 'lukas@home' });
  });

  it('turning widgets off with the switch clears the wish: no set-up after sign-in', async () => {
    const storage = memoryStorage({ [WANTED_KEY]: '1' });
    const api = fakeApi();
    const shell = fakeShell({ supported: true, configured: true, credentialId: 'cred-1' });
    await remembering(shell, api, storage).disable();
    expect(shell.calls.find((c) => c.method === 'clearWidgets')?.options).toEqual({
      signedOut: false,
    });
    expect(storage.getItem(WANTED_KEY)).toBeNull();
    const later = remembering(fakeShell({ supported: true, configured: false }), api, storage);
    await later.maintain();
    expect(api.createCredential).not.toHaveBeenCalled();
    expect(later.state).toBe('off');
  });

  it('enable remembers the wish and a configured shell backfills it', async () => {
    const storage = memoryStorage();
    await remembering(fakeShell(), fakeApi(), storage).enable();
    expect(storage.getItem(WANTED_KEY)).toBe('1');
    const other = memoryStorage();
    const shell = fakeShell({ supported: true, configured: true, credentialId: 'cred-7' });
    await remembering(shell, fakeApi(), other).maintain();
    expect(other.getItem(WANTED_KEY)).toBe('1');
    expect(held(other)).toMatchObject({ id: 'cred-7', user: 'lukas@home' });
  });

  it('remembers a revocation the server refused and retries it after the next sign-in', async () => {
    const storage = memoryStorage({ [WANTED_KEY]: '1' });
    const shell = fakeShell({ supported: true, configured: true, credentialId: 'cred-old' });
    const api = fakeApi({
      revokeCredential: vi.fn(async () => {
        throw problem(401);
      }),
    });
    await remembering(shell, api, storage).signOut();
    expect(shell.calls.some((c) => c.method === 'clearWidgets')).toBe(true);
    expect(JSON.parse(storage.getItem(REVOKE_KEY) ?? '[]')).toMatchObject([
      { id: 'cred-old', user: 'lukas@home' },
    ]);

    const signedInAgain = fakeApi();
    await remembering(fakeShell(), signedInAgain, storage).maintain();
    expect(signedInAgain.revokeCredential).toHaveBeenCalledWith('cred-old');
    expect(storage.getItem(REVOKE_KEY)).toBeNull();
  });

  it('treats a credential the server no longer knows as revoked', async () => {
    const storage = memoryStorage({ [REVOKE_KEY]: JSON.stringify(['gone']) });
    const api = fakeApi({
      revokeCredential: vi.fn(async () => {
        throw problem(404);
      }),
    });
    await remembering(fakeShell(), api, storage).maintain();
    expect(storage.getItem(REVOKE_KEY)).toBeNull();
  });

  it('turning widgets on again replaces the credential the shell lost, never adds a second', async () => {
    const storage = memoryStorage({
      [CREDENTIAL_KEY]: JSON.stringify({ id: 'cred-old', user: 'lukas@home' }),
    });
    const shell = fakeShell({ supported: true, configured: false });
    const api = fakeApi();
    expect(await remembering(shell, api, storage).enable()).toBe('on');
    expect(api.createCredential).toHaveBeenCalledWith('MowDeck iOS', 'cred-old');
    expect(held(storage)?.id).toBe('cred-1');
  });

  it('creates a fresh credential when the one to replace is already gone', async () => {
    const storage = memoryStorage({ [CREDENTIAL_KEY]: 'purged' });
    const create = vi
      .fn<WidgetsApi['createCredential']>()
      .mockRejectedValueOnce(problem(404))
      .mockResolvedValueOnce({ id: 'cred-2', token: 'mdk_x', expires_at: '2027-01-25T12:00:00Z' });
    const api = fakeApi({ createCredential: create });
    expect(await remembering(fakeShell(), api, storage).enable()).toBe('on');
    expect(create).toHaveBeenNthCalledWith(1, 'MowDeck iOS', 'purged');
    expect(create).toHaveBeenNthCalledWith(2, 'MowDeck iOS', null);
  });
});

describe('WidgetsRegistration with another user on the same device', () => {
  it('never uses or replaces the credential another user left in the shell', async () => {
    const storage = memoryStorage({
      [WANTED_KEY]: '1',
      [CREDENTIAL_KEY]: JSON.stringify({ id: 'cred-anna', user: 'anna@home' }),
    });
    const shell = fakeShell({ supported: true, configured: true, credentialId: 'cred-anna' });
    const api = fakeApi();
    await remembering(shell, api, storage, 'ben@home').maintain();
    expect(shell.calls.find((c) => c.method === 'clearWidgets')?.options).toEqual({
      signedOut: true,
    });
    expect(api.createCredential).toHaveBeenCalledWith('MowDeck iOS', null);
    expect(api.revokeCredential).not.toHaveBeenCalled(); // Anna's to revoke, not Ben's
    expect(JSON.parse(storage.getItem(REVOKE_KEY) ?? '[]')).toMatchObject([
      { id: 'cred-anna', user: 'anna@home' },
    ]);
    expect(held(storage)).toMatchObject({ id: 'cred-1', user: 'ben@home' });
  });

  it('retries a revocation only for its own user', async () => {
    const storage = memoryStorage({
      [REVOKE_KEY]: JSON.stringify([{ id: 'cred-anna', user: 'anna@home' }]),
    });
    const api = fakeApi();
    await remembering(fakeShell(), api, storage, 'ben@home').maintain();
    expect(api.revokeCredential).not.toHaveBeenCalled();
    await remembering(fakeShell(), api, storage, 'anna@home').maintain();
    expect(api.revokeCredential).toHaveBeenCalledWith('cred-anna');
    expect(storage.getItem(REVOKE_KEY)).toBeNull();
  });
});

describe('WidgetsRegistration never shows "off" while widgets are wanted', () => {
  it('sets up a credential the shell lost, replacing it on the server', async () => {
    const storage = memoryStorage({
      [CREDENTIAL_KEY]: JSON.stringify({ id: 'cred-7', user: 'lukas@home' }),
    });
    const api = fakeApi();
    const widgets = remembering(fakeShell({ supported: true, configured: false }), api, storage);
    await widgets.maintain();
    expect(api.createCredential).toHaveBeenCalledWith('MowDeck iOS', 'cred-7');
    expect(widgets.state).toBe('on');
  });

  it('is error when the set-up after sign-in fails, and tries again', async () => {
    const storage = memoryStorage({ [WANTED_KEY]: '1' });
    const create = vi
      .fn<WidgetsApi['createCredential']>()
      .mockRejectedValueOnce(problem(503))
      .mockResolvedValue({ id: 'cred-3', token: 'mdk_y', expires_at: '2027-01-25T12:00:00Z' });
    const widgets = remembering(
      fakeShell({ supported: true, configured: false }),
      fakeApi({ createCredential: create }),
      storage,
    );
    await widgets.maintain();
    expect(widgets.state).toBe('error');
    await vi.waitFor(() => expect(widgets.state).toBe('on'));
    widgets.stop();
  });

  it('is error when the shell cannot report its state, and asks again', async () => {
    const shell = fakeShell({ supported: true, configured: true, credentialId: 'cred-7' });
    const real = shell.plugin.call;
    let failures = 1;
    shell.plugin.call = async (method, options) => {
      if (method === 'getWidgetStatus' && failures-- > 0) throw new Error('bridge busy');
      return real(method, options);
    };
    const widgets = remembering(shell, fakeApi(), memoryStorage());
    await widgets.maintain();
    expect(widgets.state).toBe('error');
    await vi.waitFor(() => expect(widgets.state).toBe('on'));
    widgets.stop();
  });
});

describe('WidgetsRegistration and the login session', () => {
  it('replaces a credential of an earlier login session, keeps one of this session', async () => {
    const storage = memoryStorage({
      [CREDENTIAL_KEY]: JSON.stringify({ id: 'cred-7', user: 'lukas@home', session: 's1' }),
    });
    const shell = fakeShell({ supported: true, configured: true, credentialId: 'cred-7' });
    const same = fakeApi();
    await remembering(shell, same, storage, 'lukas@home', 's1').maintain();
    expect(same.createCredential).not.toHaveBeenCalled();

    const later = fakeApi();
    await remembering(shell, later, storage, 'lukas@home', 's2').maintain();
    expect(later.createCredential).toHaveBeenCalledWith('MowDeck iOS', 'cred-7');
    expect(held(storage)).toEqual({ id: 'cred-1', user: 'lukas@home', session: 's2' });
  });
});
