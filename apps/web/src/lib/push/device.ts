/**
 * Web Push on this device: whether the browser can do it, the subscription flow (permission,
 * `pushManager.subscribe` with the server's VAPID key, `POST /push-subscriptions`) and which of
 * the account's subscriptions is this browser's. Everything the browser provides is injected,
 * so the flow is tested with a fake `PushManager`.
 */
import { isAppleMobile, isInstalledDisplay, readInstallEnvironment } from '#lib/install/context.js';

import { sameKey, saveVapidKey, takePending, urlBase64ToUint8Array } from './store';

/** What this browser can do: `insecure` (HTTP other than localhost), `ios-install` (iOS and
 * iPadOS deliver Web Push only to a web app added to the home screen), `unsupported`. */
export type PushSupport = 'supported' | 'insecure' | 'ios-install' | 'unsupported';

export interface PushEnvironment {
  secure: boolean;
  serviceWorker: boolean;
  pushManager: boolean;
  notification: boolean;
  ios: boolean;
  standalone: boolean;
}

export function detectEnvironment(): PushEnvironment {
  const w = typeof window === 'undefined' ? undefined : window;
  const nav = typeof navigator === 'undefined' ? undefined : navigator;
  const install = readInstallEnvironment(w);
  return {
    secure: w?.isSecureContext ?? false,
    serviceWorker: !!nav && 'serviceWorker' in nav,
    pushManager: !!w && 'PushManager' in w,
    notification: !!w && 'Notification' in w,
    ios: isAppleMobile(install.userAgent, install.maxTouchPoints),
    standalone: isInstalledDisplay(install),
  };
}

export function pushSupport(env: PushEnvironment): PushSupport {
  if (!env.secure) return 'insecure';
  if (env.ios && !env.standalone && !env.pushManager) return 'ios-install';
  if (!env.serviceWorker || !env.pushManager || !env.notification) return 'unsupported';
  return 'supported';
}

export type PushFailure = 'denied' | 'no-key' | 'no-subscription';

/** The subscription flow stopped for a reason the page explains. */
export class PushError extends Error {
  constructor(readonly reason: PushFailure) {
    super(reason);
    this.name = 'PushError';
  }
}

export interface PushApi {
  /** The VAPID public key; null when the server has no Web Push (404). */
  vapidKey(): Promise<string | null>;
  add(subscription: { endpoint: string; keys: { p256dh: string; auth: string } }): Promise<{
    id: string;
  }>;
  remove(id: string): Promise<void>;
}

export interface PushDeps {
  api: PushApi;
  registration: () => Promise<ServiceWorkerRegistration>;
  requestPermission: () => Promise<NotificationPermission>;
  storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null;
  saveKey?: (key: string) => Promise<void>;
  takePending?: () => Promise<PushSubscriptionJSON | null>;
}

const DEVICE_KEY = 'mowdeck.push-subscription';

function body(json: PushSubscriptionJSON): {
  endpoint: string;
  keys: { p256dh: string; auth: string };
} {
  const p256dh = json.keys?.p256dh;
  const auth = json.keys?.auth;
  if (!json.endpoint || !p256dh || !auth) throw new PushError('no-subscription');
  return { endpoint: json.endpoint, keys: { p256dh, auth } };
}

export class PushDevice {
  readonly #deps: PushDeps;

  constructor(deps: PushDeps) {
    this.#deps = deps;
  }

  /** The id of this browser's subscription on the server (remembered after `enable`). */
  deviceId(): string | null {
    try {
      return this.#deps.storage?.getItem(DEVICE_KEY) ?? null;
    } catch {
      return null;
    }
  }

  #remember(id: string | null): void {
    try {
      if (id) this.#deps.storage?.setItem(DEVICE_KEY, id);
      else this.#deps.storage?.removeItem(DEVICE_KEY);
    } catch {
      // storage blocked: the "this device" marker is lost, nothing else
    }
  }

  /** True when the browser holds a push subscription for this app. */
  async subscribed(): Promise<boolean> {
    const registration = await this.#deps.registration();
    return (await registration.pushManager.getSubscription()) !== null;
  }

  /**
   * Ask for permission (call from a user action), subscribe with the server's key and store
   * the subscription. An existing subscription with the same key is reused.
   */
  async enable(): Promise<{ id: string }> {
    const permission = await this.#deps.requestPermission();
    if (permission !== 'granted') throw new PushError('denied');
    const key = await this.#deps.api.vapidKey();
    if (!key) throw new PushError('no-key');
    const registration = await this.#deps.registration();
    let subscription = await registration.pushManager.getSubscription();
    if (subscription && !sameKey(subscription.options?.applicationServerKey, key)) {
      await subscription.unsubscribe();
      subscription = null;
    }
    subscription ??= await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });
    const stored = await this.#deps.api.add(body(subscription.toJSON()));
    this.#remember(stored.id);
    await (this.#deps.saveKey ?? saveVapidKey)(key);
    return stored;
  }

  /** Remove a subscription; for this device's own one also unsubscribe the browser. */
  async remove(id: string): Promise<void> {
    if (id === this.deviceId()) {
      const registration = await this.#deps.registration();
      await (await registration.pushManager.getSubscription())?.unsubscribe();
      this.#remember(null);
    }
    await this.#deps.api.remove(id);
  }

  /**
   * Post a subscription the service worker renewed on `pushsubscriptionchange` (it has no token
   * to do so itself) and drop the replaced one. Returns true when one was posted.
   */
  async syncPending(): Promise<boolean> {
    const pending = await (this.#deps.takePending ?? takePending)();
    if (!pending) return false;
    const previous = this.deviceId();
    const stored = await this.#deps.api.add(body(pending));
    this.#remember(stored.id);
    if (previous && previous !== stored.id) {
      await this.#deps.api.remove(previous).catch(() => undefined);
    }
    return true;
  }
}

/** A short "Chrome · Android" description of a user agent string. */
export function describeAgent(agent: string | null | undefined): string | null {
  if (!agent) return null;
  const browser =
    [
      ['Edg/', 'Edge'],
      ['OPR/', 'Opera'],
      ['SamsungBrowser', 'Samsung Internet'],
      ['Firefox/', 'Firefox'],
      ['Chrome/', 'Chrome'],
      ['Safari/', 'Safari'],
    ].find(([needle]) => agent.includes(needle as string))?.[1] ?? null;
  const os =
    [
      ['Android', 'Android'],
      ['iPhone', 'iOS'],
      ['iPad', 'iPadOS'],
      ['Windows', 'Windows'],
      ['Mac OS X', 'macOS'],
      ['CrOS', 'ChromeOS'],
      ['Linux', 'Linux'],
    ].find(([needle]) => agent.includes(needle as string))?.[1] ?? null;
  const parts = [browser, os].filter(Boolean);
  return parts.length ? parts.join(' · ') : agent.slice(0, 40);
}
