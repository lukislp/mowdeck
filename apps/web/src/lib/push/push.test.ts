import { describe, expect, it, vi } from 'vitest';

import {
  describeAgent,
  pushSupport,
  PushDevice,
  PushError,
  type PushApi,
  type PushEnvironment,
} from './device';
import { fallbackMessage, notificationOptions, parsePushPayload, safePath } from './payload';
import { sameKey, urlBase64ToUint8Array } from './store';

// A P-256 public key as base64url (65 bytes, uncompressed point).
const KEY =
  'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U';
const OTHER_KEY =
  'BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4';

describe('push payload', () => {
  it('parses a WebPushPayload and keeps the page on this origin', () => {
    const message = parsePushPayload(
      JSON.stringify({
        title: 'Luba ist fertig',
        body: 'Gemäht in 42 min',
        url: 'https://mowdeck.example/mowers/m1?x=1',
        tag: 'mower:m1:mowing_finished',
        kind: 'mowing_finished',
        mower_id: 'm1',
        notification_id: 'n1',
      }),
    );
    expect(message).toEqual({
      title: 'Luba ist fertig',
      body: 'Gemäht in 42 min',
      url: '/mowers/m1?x=1',
      tag: 'mower:m1:mowing_finished',
      kind: 'mowing_finished',
      mowerId: 'm1',
      notificationId: 'n1',
    });
    const options = notificationOptions(message!);
    expect(options.tag).toBe('mower:m1:mowing_finished');
    expect(options.data).toMatchObject({ url: '/mowers/m1?x=1', notificationId: 'n1' });
  });

  it('rejects what is not a payload and falls back to safe pages', () => {
    expect(parsePushPayload('not json')).toBeNull();
    expect(parsePushPayload({ body: 'no title' })).toBeNull();
    expect(parsePushPayload(null)).toBeNull();
    expect(parsePushPayload({ title: 'T' })?.url).toBe('/notifications');
    expect(parsePushPayload({ title: 'T', mower_id: 'm 2' })?.url).toBe('/mowers/m%202');
    expect(safePath('//evil.example/x', null)).toBe('/notifications');
    expect(safePath('javascript:alert(1)', null)).toBe('/notifications');
    expect(safePath('not a url', 'm1')).toBe('/mowers/m1');
    expect(safePath('/mowers/m1', null)).toBe('/mowers/m1');
    expect(fallbackMessage().title).toBe('MowDeck');
    expect(notificationOptions(fallbackMessage()).tag).toBeUndefined();
  });
});

describe('push support', () => {
  const base: PushEnvironment = {
    secure: true,
    serviceWorker: true,
    pushManager: true,
    notification: true,
    ios: false,
    standalone: false,
  };

  it('tells why push is not possible', () => {
    expect(pushSupport(base)).toBe('supported');
    expect(pushSupport({ ...base, secure: false })).toBe('insecure');
    expect(pushSupport({ ...base, ios: true, pushManager: false })).toBe('ios-install');
    expect(pushSupport({ ...base, ios: true, standalone: true })).toBe('supported');
    expect(pushSupport({ ...base, pushManager: false })).toBe('unsupported');
    expect(pushSupport({ ...base, serviceWorker: false })).toBe('unsupported');
  });

  it('describes user agents briefly', () => {
    expect(
      describeAgent(
        'Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 Chrome/128.0 Mobile Safari/537.36',
      ),
    ).toBe('Chrome · Android');
    expect(
      describeAgent('Mozilla/5.0 (iPhone; CPU iPhone OS 17_5) Version/17.5 Safari/604.1'),
    ).toBe('Safari · iOS');
    expect(describeAgent(null)).toBeNull();
    expect(describeAgent('curl/8')).toBe('curl/8');
  });

  it('converts keys', () => {
    const bytes = urlBase64ToUint8Array(KEY);
    expect(bytes.length).toBe(65);
    expect(bytes[0]).toBe(4);
    expect(sameKey(bytes.buffer, KEY)).toBe(true);
    expect(sameKey(bytes.buffer, OTHER_KEY)).toBe(false);
    expect(sameKey(null, KEY)).toBe(false);
  });
});

/** A fake PushManager with the parts the flow uses. */
class FakePushManager {
  current: FakeSubscription | null = null;
  subscribe = vi.fn(async (options: PushSubscriptionOptionsInit) => {
    this.current = new FakeSubscription(this, options.applicationServerKey as Uint8Array);
    return this.current as unknown as PushSubscription;
  });
  getSubscription = vi.fn(async () => this.current as unknown as PushSubscription | null);
}

let counter = 0;

class FakeSubscription {
  readonly endpoint: string;
  readonly options: { applicationServerKey: ArrayBuffer };
  constructor(
    private readonly manager: FakePushManager,
    key: Uint8Array,
  ) {
    counter += 1;
    this.endpoint = `https://fcm.googleapis.com/fcm/send/device-${counter}`;
    this.options = { applicationServerKey: key.slice().buffer };
  }
  unsubscribe = vi.fn(async () => {
    this.manager.current = null;
    return true;
  });
  toJSON(): PushSubscriptionJSON {
    return { endpoint: this.endpoint, keys: { p256dh: 'p256', auth: 'auth' } };
  }
}

function setup(permission: NotificationPermission = 'granted', key: string | null = KEY) {
  const manager = new FakePushManager();
  const stored = new Map<string, string>();
  const api: PushApi & { add: ReturnType<typeof vi.fn>; remove: ReturnType<typeof vi.fn> } = {
    vapidKey: vi.fn(async () => key),
    add: vi.fn(async (subscription: { endpoint: string }) => ({
      id: `id-${subscription.endpoint.split('-').pop()}`,
    })),
    remove: vi.fn(async () => undefined),
  };
  const saveKey = vi.fn(async () => undefined);
  let pending: PushSubscriptionJSON | null = null;
  const device = new PushDevice({
    api,
    registration: async () => ({ pushManager: manager }) as unknown as ServiceWorkerRegistration,
    requestPermission: vi.fn(async () => permission),
    storage: {
      getItem: (k) => stored.get(k) ?? null,
      setItem: (k, v) => void stored.set(k, v),
      removeItem: (k) => void stored.delete(k),
    },
    saveKey,
    takePending: async () => {
      const value = pending;
      pending = null;
      return value;
    },
  });
  return {
    manager,
    api,
    device,
    saveKey,
    setPending: (p: PushSubscriptionJSON) => (pending = p),
  };
}

describe('PushDevice', () => {
  it('subscribes with the server key, posts the subscription and remembers this device', async () => {
    const { manager, api, device, saveKey } = setup();
    const stored = await device.enable();
    expect(manager.subscribe).toHaveBeenCalledTimes(1);
    const options = manager.subscribe.mock.calls[0]![0];
    expect(options.userVisibleOnly).toBe(true);
    expect(sameKey((options.applicationServerKey as Uint8Array).buffer as ArrayBuffer, KEY)).toBe(
      true,
    );
    expect(api.add).toHaveBeenCalledWith({
      endpoint: expect.stringContaining('https://fcm.googleapis.com/'),
      keys: { p256dh: 'p256', auth: 'auth' },
    });
    expect(device.deviceId()).toBe(stored.id);
    expect(saveKey).toHaveBeenCalledWith(KEY);
    expect(await device.subscribed()).toBe(true);

    // Enabling again reuses the subscription of the same key.
    await device.enable();
    expect(manager.subscribe).toHaveBeenCalledTimes(1);
  });

  it('replaces a subscription made with another key', async () => {
    const { manager, device } = setup();
    await manager.subscribe({ applicationServerKey: urlBase64ToUint8Array(OTHER_KEY) });
    const old = manager.current!;
    await device.enable();
    expect(old.unsubscribe).toHaveBeenCalled();
    expect(manager.subscribe).toHaveBeenCalledTimes(2);
  });

  it('stops when permission is denied or the server has no Web Push', async () => {
    const denied = setup('denied');
    await expect(denied.device.enable()).rejects.toEqual(new PushError('denied'));
    expect(denied.manager.subscribe).not.toHaveBeenCalled();
    const keyless = setup('granted', null);
    await expect(keyless.device.enable()).rejects.toMatchObject({ reason: 'no-key' });
  });

  it('removes this device locally and on the server, others only on the server', async () => {
    const { manager, api, device } = setup();
    const own = await device.enable();
    await device.remove('someone-else');
    expect(manager.current).not.toBeNull();
    await device.remove(own.id);
    expect(manager.current).toBeNull();
    expect(device.deviceId()).toBeNull();
    expect(api.remove).toHaveBeenCalledWith(own.id);
  });

  it('posts a subscription the worker renewed and drops the replaced one', async () => {
    const { api, device, setPending } = setup();
    const before = await device.enable();
    expect(await device.syncPending()).toBe(false);
    setPending({
      endpoint: 'https://fcm.googleapis.com/fcm/send/device-99',
      keys: { p256dh: 'a', auth: 'b' },
    });
    expect(await device.syncPending()).toBe(true);
    expect(device.deviceId()).toBe('id-99');
    expect(api.remove).toHaveBeenCalledWith(before.id);
    setPending({ endpoint: '' });
    await expect(device.syncPending()).rejects.toMatchObject({ reason: 'no-subscription' });
  });
});
