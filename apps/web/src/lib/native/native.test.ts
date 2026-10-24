import { describe, expect, it, vi } from 'vitest';

import { ApnsRegistration } from './apns';
import { nativeBridge, type CapacitorBridge, type NativeBridge } from './capacitor';
import { pathFromDeepLink, pathFromNotificationAction } from './links';
import { InboxNotifier, notificationNumber, toLocalNotification } from './local';
import { NativeShell } from './shell.svelte';
import type { InboxItem } from '$lib/stores/inbox.svelte';

// --- a fake of the injected window.Capacitor ------------------------------------------------

type Handler = (method: string, options: object) => unknown;

class FakeCapacitor implements CapacitorBridge {
  calls: { plugin: string; method: string; options: object }[] = [];
  listeners = new Map<string, ((data: unknown) => void)[]>();
  handlers = new Map<string, Handler>();
  PluginHeaders: { name: string }[];

  constructor(
    readonly platform: string,
    plugins: string[],
  ) {
    this.PluginHeaders = plugins.map((name) => ({ name }));
  }

  isNativePlatform = () => true;
  getPlatform = () => this.platform;
  nativePromise = (plugin: string, method: string, options: object = {}) => {
    this.calls.push({ plugin, method, options });
    const handler = this.handlers.get(plugin);
    return Promise.resolve(handler ? handler(method, options) : undefined);
  };
  addListener = (plugin: string, event: string, callback: (data: unknown) => void) => {
    const key = `${plugin}.${event}`;
    this.listeners.set(key, [...(this.listeners.get(key) ?? []), callback]);
    return {
      remove: () =>
        this.listeners.set(
          key,
          (this.listeners.get(key) ?? []).filter((c) => c !== callback),
        ),
    };
  };

  emit(plugin: string, event: string, data: unknown): void {
    for (const callback of this.listeners.get(`${plugin}.${event}`) ?? []) callback(data);
  }

  called(plugin: string, method: string) {
    return this.calls.filter((c) => c.plugin === plugin && c.method === method);
  }
}

function memoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (key) => data.get(key) ?? null,
    key: (index) => [...data.keys()][index] ?? null,
    removeItem: (key) => void data.delete(key),
    setItem: (key, value) => void data.set(key, value),
  };
}

function item(id: string, extra: Partial<InboxItem> = {}): InboxItem {
  return {
    id,
    mower_id: 'mower-1',
    kind: 'mower_stuck',
    severity: 'critical',
    title: `Title ${id}`,
    body: 'Body',
    url: '/mowers/mower-1',
    occurred_at: '2026-10-11T10:00:00Z',
    created_at: '2026-10-11T10:00:00Z',
    read: false,
    read_at: null,
    acked: false,
    acked_at: null,
    ...extra,
  };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

// --- detection ----------------------------------------------------------------------------

describe('nativeBridge', () => {
  it('is null in a browser', () => {
    expect(nativeBridge({})).toBeNull();
    expect(nativeBridge(undefined)).toBeNull();
    expect(nativeBridge({ Capacitor: { isNativePlatform: () => false } })).toBeNull();
  });

  it('is null for an unknown platform or an incomplete bridge', () => {
    const web = new FakeCapacitor('web', []);
    expect(nativeBridge({ Capacitor: web })).toBeNull();
    const broken = { isNativePlatform: () => true, getPlatform: () => 'ios' };
    expect(nativeBridge({ Capacitor: broken })).toBeNull();
  });

  it('detects the shell and calls plugins through the injected bridge', async () => {
    const cap = new FakeCapacitor('android', ['App']);
    cap.handlers.set('App', () => ({ url: 'mowdeck://x' }));
    const bridge = nativeBridge({ Capacitor: cap });
    expect(bridge?.platform).toBe('android');
    expect(bridge?.has('App')).toBe(true);
    expect(bridge?.has('PushNotifications')).toBe(false);
    expect(await bridge?.plugin('App').call('getLaunchUrl')).toEqual({ url: 'mowdeck://x' });
    const seen: unknown[] = [];
    const off = bridge!.plugin('App').on('appUrlOpen', (data) => seen.push(data));
    cap.emit('App', 'appUrlOpen', 1);
    off();
    cap.emit('App', 'appUrlOpen', 2);
    expect(seen).toEqual([1]);
  });
});

// --- links --------------------------------------------------------------------------------

describe('pathFromDeepLink', () => {
  it.each([
    ['mowdeck://mowers/abc', '/mowers/abc'],
    ['mowdeck:///mowers/abc', '/mowers/abc'],
    ['mowdeck://mowers/abc/', '/mowers/abc'],
    ['mowdeck://vision/proposals?x=1#top', '/vision/proposals?x=1#top'],
    ['mowdeck://notifications', '/notifications'],
    ['mowdeck://', '/'],
  ])('%s opens %s', (link, path) => {
    expect(pathFromDeepLink(link)).toBe(path);
  });

  it.each(['https://evil.example/mowers', 'javascript:alert(1)', 'not a url', 'ntfy://x/y'])(
    'ignores %s',
    (link) => {
      expect(pathFromDeepLink(link)).toBeNull();
    },
  );
});

describe('pathFromNotificationAction', () => {
  it('reads a local notification and an APNs message', () => {
    expect(
      pathFromNotificationAction({ notification: { extra: { url: '/mowers/1/map?x=1' } } }),
    ).toBe('/mowers/1/map?x=1');
    expect(
      pathFromNotificationAction({
        notification: { data: { url: 'https://mowdeck.lan/mowers/2', aps: {} } },
      }),
    ).toBe('/mowers/2');
  });

  it('falls back to the mower page or the inbox, never a foreign site', () => {
    expect(pathFromNotificationAction({ notification: { data: { mower_id: 'm9' } } })).toBe(
      '/mowers/m9',
    );
    expect(pathFromNotificationAction({ notification: { extra: { url: '//evil' } } })).toBe(
      '/notifications',
    );
    expect(pathFromNotificationAction(null)).toBe('/notifications');
  });
});

// --- local notifications ------------------------------------------------------------------

describe('InboxNotifier', () => {
  it('sets a baseline on the first start, then notifies new unread items once', () => {
    const storage = memoryStorage();
    const notifier = new InboxNotifier(storage);
    expect(notifier.fresh([item('a'), item('b')])).toEqual([]);
    expect(notifier.fresh([item('c'), item('d', { read: true }), item('a')])).toEqual([item('c')]);
    expect(notifier.fresh([item('c'), item('a')])).toEqual([]);
    // A restart remembers what was seen: no baseline, no repeats.
    const again = new InboxNotifier(storage);
    expect(again.fresh([item('e'), item('c')]).map((i) => i.id)).toEqual(['e']);
  });

  it('returns several new items oldest first', () => {
    const notifier = new InboxNotifier(memoryStorage());
    notifier.fresh([]);
    expect(notifier.fresh([item('new'), item('old')]).map((i) => i.id)).toEqual(['old', 'new']);
  });

  it('works without storage', () => {
    const notifier = new InboxNotifier(null);
    expect(notifier.fresh([item('a')])).toEqual([]);
    expect(notifier.fresh([item('b'), item('a')]).map((i) => i.id)).toEqual(['b']);
  });

  it('maps an item to a local notification', () => {
    const id = '7f3a91c2-0000-4000-8000-000000000000';
    expect(notificationNumber(id)).toBe(0x7f3a91c);
    expect(notificationNumber('zz')).toBe(0);
    const local = toLocalNotification(item(id));
    expect(local).toMatchObject({
      id: 0x7f3a91c,
      title: `Title ${id}`,
      channelId: 'mowdeck-alarms',
      group: 'mower:mower-1',
      extra: { url: '/mowers/mower-1', notification_id: id, kind: 'mower_stuck' },
    });
    expect(toLocalNotification(item('b', { severity: 'info', mower_id: null })).channelId).toBe(
      'mowdeck',
    );
  });
});

// --- APNs ---------------------------------------------------------------------------------

const INSTALLATION = '6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b';

function apnsSetup(
  receive: string,
  requested = receive,
  installationId?: () => Promise<string | null>,
) {
  const cap = new FakeCapacitor('ios', ['PushNotifications']);
  cap.handlers.set('PushNotifications', (method) => {
    if (method === 'checkPermissions') return { receive };
    if (method === 'requestPermissions') return { receive: requested };
    return undefined;
  });
  const bridge = nativeBridge({ Capacitor: cap }) as NativeBridge;
  const api = {
    add: vi.fn().mockResolvedValue({ id: 'device-1' }),
    remove: vi.fn().mockResolvedValue(undefined),
  };
  const storage = memoryStorage();
  const states: string[] = [];
  const registration = new ApnsRegistration({
    push: bridge.plugin('PushNotifications'),
    api,
    storage,
    deviceName: 'MowDeck iOS',
    installationId,
    onChange: (state) => states.push(state),
  });
  return { cap, api, storage, states, registration };
}

describe('ApnsRegistration', () => {
  it('asks, registers and stores the token on the server', async () => {
    const { cap, api, registration, states } = apnsSetup('prompt', 'granted');
    expect(await registration.enable()).toBe('pending');
    expect(cap.called('PushNotifications', 'requestPermissions')).toHaveLength(1);
    expect(cap.called('PushNotifications', 'register')).toHaveLength(1);
    cap.emit('PushNotifications', 'registration', { value: 'ABCDEF0123' });
    await flush();
    expect(api.add).toHaveBeenCalledWith('ABCDEF0123', 'MowDeck iOS', null);
    expect(registration.deviceId()).toBe('device-1');
    expect(registration.state).toBe('on');
    expect(states).toEqual(['pending', 'on']);
  });

  it('sends the installation id so a new token replaces the old one', async () => {
    const id = vi.fn().mockResolvedValue(INSTALLATION.toUpperCase());
    const { cap, api, registration } = apnsSetup('granted', 'granted', id);
    await registration.enable();
    cap.emit('PushNotifications', 'registration', { value: 'AA' });
    await flush();
    cap.emit('PushNotifications', 'registration', { value: 'BB' });
    await flush();
    expect(api.add.mock.calls).toEqual([
      ['AA', 'MowDeck iOS', INSTALLATION],
      ['BB', 'MowDeck iOS', INSTALLATION],
    ]);
    expect(registration.state).toBe('on');
  });

  it('registers without an installation id when the shell has none or a bad one', async () => {
    for (const id of [
      () => Promise.reject(new Error('UNIMPLEMENTED')), // shell build before the method
      () => Promise.resolve('not-a-uuid'),
      () => Promise.resolve(null),
    ]) {
      const { cap, api, registration } = apnsSetup('granted', 'granted', id);
      await registration.enable();
      cap.emit('PushNotifications', 'registration', { value: 'AA' });
      await flush();
      expect(api.add).toHaveBeenCalledWith('AA', 'MowDeck iOS', null);
      expect(registration.state).toBe('on');
    }
  });

  it('does not ask again when permission was granted and listens once', async () => {
    const { cap, registration } = apnsSetup('granted');
    await registration.enable();
    await registration.enable();
    expect(cap.called('PushNotifications', 'requestPermissions')).toHaveLength(0);
    expect(cap.listeners.get('PushNotifications.registration')).toHaveLength(1);
  });

  it('reports a denied permission without registering', async () => {
    const { cap, registration } = apnsSetup('denied');
    expect(await registration.enable()).toBe('denied');
    expect(cap.called('PushNotifications', 'register')).toHaveLength(0);
  });

  it('reports registration errors and failed uploads', async () => {
    const { cap, api, registration } = apnsSetup('granted');
    await registration.enable();
    cap.emit('PushNotifications', 'registrationError', { error: 'no network' });
    expect(registration.state).toBe('error');
    api.add.mockRejectedValueOnce(new Error('500'));
    cap.emit('PushNotifications', 'registration', { value: 'AA' });
    await flush();
    expect(registration.state).toBe('error');
    cap.emit('PushNotifications', 'registration', {});
    expect(registration.state).toBe('error');
  });

  it('removes the token on logout', async () => {
    const { cap, api, registration } = apnsSetup('granted');
    await registration.enable();
    cap.emit('PushNotifications', 'registration', { value: 'AA' });
    await flush();
    await registration.disable();
    expect(api.remove).toHaveBeenCalledWith('device-1');
    expect(registration.deviceId()).toBeNull();
    expect(registration.state).toBe('idle');
    await registration.disable();
    expect(api.remove).toHaveBeenCalledTimes(1);
    registration.stop();
    expect(cap.listeners.get('PushNotifications.registration')).toHaveLength(0);
  });
});

// --- the shell ------------------------------------------------------------------------------

function shellSetup(platform: 'ios' | 'android', options: { ntfy?: boolean } = {}) {
  const plugins = ['App', 'LocalNotifications', 'SplashScreen', 'Badge', 'MowDeckShell'];
  if (platform === 'ios') plugins.push('PushNotifications');
  const cap = new FakeCapacitor(platform, plugins);
  cap.handlers.set('LocalNotifications', (method) =>
    method === 'checkPermissions' ? { display: 'prompt' } : { display: 'granted' },
  );
  cap.handlers.set('PushNotifications', (method) =>
    method === 'checkPermissions' ? { receive: 'granted' } : undefined,
  );
  cap.handlers.set('App', () => ({ url: 'mowdeck://mowers/launch' }));
  cap.handlers.set('MowDeckShell', (method) =>
    method === 'getInstallationId' ? { id: INSTALLATION } : { url: 'https://mowdeck.lan' },
  );
  const api = {
    add: vi.fn().mockResolvedValue({ id: 'device-1' }),
    remove: vi.fn().mockResolvedValue(undefined),
  };
  const shell = new NativeShell({
    bridge: nativeBridge({ Capacitor: cap }),
    storage: memoryStorage(),
    apns: api,
    ntfyConfigured: vi.fn().mockResolvedValue(options.ntfy ?? false),
  });
  return { cap, api, shell };
}

describe('NativeShell', () => {
  it('is inert in a browser', async () => {
    const shell = new NativeShell({
      bridge: null,
      storage: null,
      apns: { add: vi.fn(), remove: vi.fn() },
      ntfyConfigured: vi.fn(),
    });
    expect(shell.active).toBe(false);
    expect(shell.platform).toBeNull();
    expect(shell.apnsAvailable).toBe(false);
    shell.start(vi.fn())();
    await shell.signedIn();
    await shell.inboxChanged([item('a')], 1);
    await shell.signingOut();
    await shell.changeServer();
  });

  it('routes deep links, the launch URL and notification taps', async () => {
    const { cap, shell } = shellSetup('ios');
    const navigate = vi.fn();
    const stop = shell.start(navigate);
    await flush();
    expect(navigate).toHaveBeenCalledWith('/mowers/launch');
    expect(shell.server).toBe('https://mowdeck.lan');
    expect(cap.called('SplashScreen', 'hide')).toHaveLength(1);
    cap.emit('App', 'appUrlOpen', { url: 'mowdeck://notifications' });
    cap.emit('App', 'appUrlOpen', { url: 'https://elsewhere' });
    cap.emit('LocalNotifications', 'localNotificationActionPerformed', {
      notification: { extra: { url: '/mowers/1' } },
    });
    cap.emit('PushNotifications', 'pushNotificationActionPerformed', {
      notification: { data: { url: '/mowers/2' } },
    });
    expect(navigate.mock.calls.map((c) => c[0])).toEqual([
      '/mowers/launch',
      '/notifications',
      '/mowers/1',
      '/mowers/2',
    ]);
    stop();
    cap.emit('App', 'appUrlOpen', { url: 'mowdeck://mowers/late' });
    expect(navigate).toHaveBeenCalledTimes(4);
  });

  it('iOS: registers APNs and notifies locally only while APNs is not on', async () => {
    const { cap, api, shell } = shellSetup('ios');
    await shell.signedIn();
    await shell.signedIn();
    expect(shell.apnsAvailable).toBe(true);
    expect(cap.called('PushNotifications', 'register')).toHaveLength(1);
    expect(shell.local).toBe(true);
    await shell.inboxChanged([item('a')], 1); // baseline
    await shell.inboxChanged([item('b'), item('a')], 2);
    expect(cap.called('LocalNotifications', 'schedule')).toHaveLength(1);
    expect(cap.called('Badge', 'set').at(-1)?.options).toEqual({ count: 2 });
    cap.emit('PushNotifications', 'registration', { value: 'AA' });
    await flush();
    expect(shell.apns).toBe('on');
    expect(api.add).toHaveBeenCalledWith('AA', 'MowDeck iOS', INSTALLATION);
    expect(cap.called('MowDeckShell', 'getInstallationId')).toHaveLength(1);
    await shell.inboxChanged([item('c'), item('b'), item('a')], 3);
    expect(cap.called('LocalNotifications', 'schedule')).toHaveLength(1);
  });

  it('iOS: an older shell without getInstallationId still registers', async () => {
    const { cap, api, shell } = shellSetup('ios');
    cap.handlers.set('MowDeckShell', (method) => {
      if (method === 'getInstallationId') throw new Error('UNIMPLEMENTED');
      return { url: 'https://mowdeck.lan' };
    });
    await shell.signedIn();
    cap.emit('PushNotifications', 'registration', { value: 'AA' });
    await flush();
    expect(api.add).toHaveBeenCalledWith('AA', 'MowDeck iOS', null);
    expect(shell.apns).toBe('on');
  });

  it('Android: channels, local notifications unless ntfy delivers', async () => {
    const { cap, shell } = shellSetup('android');
    await shell.signedIn();
    expect(shell.apnsAvailable).toBe(false);
    expect(cap.called('LocalNotifications', 'requestPermissions')).toHaveLength(1);
    expect(cap.called('LocalNotifications', 'createChannel')).toHaveLength(2);
    await shell.inboxChanged([], 0);
    await shell.inboxChanged([item('a')], 1);
    const scheduled = cap.called('LocalNotifications', 'schedule');
    expect(scheduled).toHaveLength(1);
    expect(scheduled[0]?.options).toMatchObject({
      notifications: [{ title: 'Title a', channelId: 'mowdeck-alarms' }],
    });

    const withNtfy = shellSetup('android', { ntfy: true });
    await withNtfy.shell.signedIn();
    expect(withNtfy.shell.ntfy).toBe(true);
    await withNtfy.shell.inboxChanged([], 0);
    await withNtfy.shell.inboxChanged([item('a')], 1);
    expect(withNtfy.cap.called('LocalNotifications', 'schedule')).toHaveLength(0);
  });

  it('removes the APNs token and clears the badge on logout; changes the server', async () => {
    const { cap, api, shell } = shellSetup('ios');
    await shell.signedIn();
    cap.emit('PushNotifications', 'registration', { value: 'AA' });
    await flush();
    await shell.signingOut();
    expect(api.remove).toHaveBeenCalledWith('device-1');
    expect(cap.called('Badge', 'clear')).toHaveLength(1);
    await shell.inboxChanged([item('z')], 1);
    expect(cap.called('Badge', 'set')).toHaveLength(0);
    await shell.changeServer();
    expect(cap.called('MowDeckShell', 'resetServerUrl')).toHaveLength(1);
    await shell.refreshNtfy();
    await shell.enableApns();
  });
});
