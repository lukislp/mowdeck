/**
 * APNs registration of the iOS shell (direct to Apple, no Firebase; ADR 0036).
 *
 * `@capacitor/push-notifications` asks for permission and registers with APNs; its
 * `registration` event carries the device token (hex), which is stored on the server
 * (`POST /api/v1/notifications/me/push/apns`). The server then sends to Apple with the owner's
 * APNs auth key while the app is in the background or closed. The device id is remembered so
 * logout can remove the token again (`DELETE .../me/push/apns/{id}`).
 */
import type { NativePlugin } from './capacitor';

const DEVICE_KEY = 'mowdeck.apns-device';

export type ApnsState = 'idle' | 'pending' | 'on' | 'denied' | 'error';

export interface ApnsApi {
  add(token: string, deviceName: string | null): Promise<{ id: string }>;
  remove(id: string): Promise<void>;
}

export interface ApnsDeps {
  push: NativePlugin;
  api: ApnsApi;
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null;
  deviceName?: string | null;
  onChange?: (state: ApnsState) => void;
}

type PermissionState = 'granted' | 'denied' | 'prompt' | 'prompt-with-rationale';

export class ApnsRegistration {
  readonly #deps: ApnsDeps;
  #state: ApnsState = 'idle';
  #offs: (() => void)[] = [];

  constructor(deps: ApnsDeps) {
    this.#deps = deps;
  }

  get state(): ApnsState {
    return this.#state;
  }

  /** The id of this device's token on the server (remembered after a registration). */
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
      // storage blocked: logout cannot remove the token, the server drops it once Apple says so
    }
  }

  #set(state: ApnsState): void {
    this.#state = state;
    this.#deps.onChange?.(state);
  }

  /**
   * Ask for permission (iOS shows its prompt once; later calls answer at once) and register.
   * Resolves when the request is under way; the token arrives with the `registration` event.
   */
  async enable(): Promise<ApnsState> {
    const { push } = this.#deps;
    this.#listen();
    this.#set('pending');
    try {
      let { receive } = await push.call<{ receive: PermissionState }>('checkPermissions');
      if (receive === 'prompt' || receive === 'prompt-with-rationale') {
        ({ receive } = await push.call<{ receive: PermissionState }>('requestPermissions'));
      }
      if (receive !== 'granted') {
        this.#set('denied');
        return this.#state;
      }
      await push.call('register');
    } catch {
      this.#set('error');
    }
    return this.#state;
  }

  /** Remove this device's token from the server (logout); APNs itself stays registered. */
  async disable(): Promise<void> {
    const id = this.deviceId();
    this.#remember(null);
    this.#set('idle');
    if (id) await this.#deps.api.remove(id).catch(() => undefined);
  }

  stop(): void {
    for (const off of this.#offs) off();
    this.#offs = [];
  }

  #listen(): void {
    if (this.#offs.length) return;
    const { push } = this.#deps;
    this.#offs.push(
      push.on('registration', (data) => {
        const token = (data as { value?: unknown } | null)?.value;
        if (typeof token !== 'string' || !token) {
          this.#set('error');
          return;
        }
        void this.#deps.api
          .add(token, this.#deps.deviceName ?? null)
          .then((stored) => {
            this.#remember(stored.id);
            this.#set('on');
          })
          .catch(() => this.#set('error'));
      }),
      push.on('registrationError', () => this.#set('error')),
    );
  }
}
