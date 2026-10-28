/**
 * Widgets and the mowing Live Activity of the iOS app (ADR 0059).
 *
 * The widgets run in an extension that must work without the app, so they cannot use the web
 * app's session. This class gives the device its own credential: it asks the server for a
 * widget credential (`POST /me/widget-credentials`, an API token limited to seeing and
 * commanding mowers, 90 days), hands it to the shell (`MowDeckShell.configureWidgets`, which
 * keeps it in the Keychain shared with the extension) and keeps it fresh. It also registers the
 * ActivityKit tokens the shell reports (the push-to-start token of the device and the update
 * token of each running Live Activity) with the Notifications service.
 *
 * Inert when the shell has no widget support (Android, a browser, older iOS builds).
 */
import type { NativePlugin } from './capacitor';

export type WidgetsState = 'unsupported' | 'off' | 'pending' | 'on' | 'error';

export interface MowerName {
  id: string;
  name: string;
}

export interface CreatedCredential {
  id: string;
  token: string;
  expires_at: string;
}

export interface WidgetsApi {
  createCredential(deviceName: string, replaces: string | null): Promise<CreatedCredential>;
  revokeCredential(id: string): Promise<void>;
  mowers(): Promise<MowerName[]>;
  registerDevice(body: {
    installation_id: string;
    push_to_start_token: string | null;
    widgets_enabled: boolean;
  }): Promise<void>;
  unregisterDevice(installationId: string): Promise<void>;
  reportActivity(
    mowerId: string,
    body: { installation_id: string; activity_id: string; push_token: string },
  ): Promise<void>;
  endActivity(mowerId: string, installationId: string): Promise<void>;
}

export interface WidgetsDeps {
  shell: NativePlugin;
  api: WidgetsApi;
  /** The server origin the shell loads. */
  serverUrl: () => string | null;
  deviceName: string;
  onChange?: (state: WidgetsState) => void;
}

interface NativeStatus {
  supported?: boolean;
  configured?: boolean;
  credentialId?: string | null;
  needsRotation?: boolean;
  activitiesEnabled?: boolean;
  pushToStartToken?: string | null;
}

interface PendingItem {
  mowerId: string;
  installationId: string;
  activityId: string;
  pushToken: string;
}

/** Swift's `ISO8601DateFormatter` wants whole seconds. */
export function wholeSeconds(iso: string): string {
  return new Date(iso).toISOString().replace(/\.\d+Z$/, 'Z');
}

export class WidgetsRegistration {
  readonly #deps: WidgetsDeps;
  #state: WidgetsState = 'off';
  #offs: (() => void)[] = [];
  #installation: string | null = null;

  constructor(deps: WidgetsDeps) {
    this.#deps = deps;
  }

  get state(): WidgetsState {
    return this.#state;
  }

  #set(state: WidgetsState): void {
    this.#state = state;
    this.#deps.onChange?.(state);
  }

  async #status(): Promise<NativeStatus> {
    return (await this.#deps.shell.call<NativeStatus>('getWidgetStatus')) ?? {};
  }

  async #installationId(): Promise<string | null> {
    if (this.#installation) return this.#installation;
    const result = await this.#deps.shell.call<{ id?: unknown }>('getInstallationId');
    this.#installation = typeof result?.id === 'string' ? result.id : null;
    return this.#installation;
  }

  /** The state of this device: are widgets set up? Call once after sign-in. */
  async refresh(): Promise<WidgetsState> {
    try {
      const status = await this.#status();
      if (status.supported === false) this.#set('unsupported');
      else this.#set(status.configured ? 'on' : 'off');
    } catch {
      this.#set('unsupported');
    }
    return this.#state;
  }

  /** Turn widgets on: credential, mower list, device registration. */
  async enable(): Promise<WidgetsState> {
    this.#set('pending');
    try {
      const server = this.#deps.serverUrl();
      if (!server) throw new Error('no server');
      const status = await this.#status();
      const created = await this.#deps.api.createCredential(
        this.#deps.deviceName,
        status.credentialId ?? null,
      );
      await this.#deps.shell.call('configureWidgets', {
        serverUrl: server,
        secret: created.token,
        credentialId: created.id,
        expiresAt: wholeSeconds(created.expires_at),
        deviceName: this.#deps.deviceName,
        mowers: await this.#deps.api.mowers().catch(() => []),
      });
      this.#listen();
      await this.#register();
      this.#set('on');
    } catch {
      this.#set('error');
    }
    return this.#state;
  }

  /** Turn widgets off: forget the device on the server, revoke the credential, clear it. */
  async disable(): Promise<void> {
    try {
      const status = await this.#status();
      const installation = await this.#installationId();
      if (installation) await this.#deps.api.unregisterDevice(installation).catch(() => undefined);
      if (status.credentialId) {
        await this.#deps.api.revokeCredential(status.credentialId).catch(() => undefined);
      }
      await this.#deps.shell.call('clearWidgets');
    } finally {
      this.#set('off');
    }
  }

  /**
   * On every start of a signed-in app with widgets on: replace a credential that expires within
   * 30 days, refresh the mower list, register the device and hand over the Live Activity tokens
   * the shell collected while the web app was not running.
   */
  async maintain(): Promise<void> {
    const status = await this.#status().catch(() => null);
    if (!status?.configured) {
      this.#set(status?.supported === false ? 'unsupported' : 'off');
      return;
    }
    this.#set('on');
    this.#listen();
    try {
      if (status.needsRotation) {
        const server = this.#deps.serverUrl();
        if (server) {
          const created = await this.#deps.api.createCredential(
            this.#deps.deviceName,
            status.credentialId ?? null,
          );
          await this.#deps.shell.call('configureWidgets', {
            serverUrl: server,
            secret: created.token,
            credentialId: created.id,
            expiresAt: wholeSeconds(created.expires_at),
            deviceName: this.#deps.deviceName,
            mowers: await this.#deps.api.mowers().catch(() => []),
          });
        }
      } else {
        const mowers = await this.#deps.api.mowers().catch(() => null);
        if (mowers) await this.#deps.shell.call('setWidgetMowers', { mowers });
      }
      await this.#register();
    } catch {
      // The widgets keep working with what they have; the next start tries again.
    }
  }

  stop(): void {
    for (const off of this.#offs) off();
    this.#offs = [];
  }

  async #register(): Promise<void> {
    const installation = await this.#installationId();
    if (!installation) return;
    const status = await this.#status();
    await this.#deps.api.registerDevice({
      installation_id: installation,
      push_to_start_token: status.pushToStartToken ?? null,
      widgets_enabled: true,
    });
    await this.#flushPending();
  }

  async #flushPending(): Promise<void> {
    const result = await this.#deps.shell.call<{ items?: PendingItem[] }>(
      'getPendingActivityRegistrations',
    );
    for (const item of result?.items ?? []) await this.#report(item);
  }

  async #report(item: PendingItem): Promise<void> {
    try {
      await this.#deps.api.reportActivity(item.mowerId, {
        installation_id: item.installationId,
        activity_id: item.activityId,
        push_token: item.pushToken,
      });
      await this.#deps.shell.call('clearPendingActivityRegistration', { mowerId: item.mowerId });
    } catch {
      // stays pending; the next start or token event reports it again
    }
  }

  #listen(): void {
    if (this.#offs.length) return;
    const { shell } = this.#deps;
    this.#offs.push(
      shell.on('pushToStartToken', () => {
        void this.#register().catch(() => undefined);
      }),
      shell.on('liveActivityToken', (data) => {
        const item = data as Partial<PendingItem> | null;
        if (item?.mowerId && item.installationId && item.activityId && item.pushToken) {
          void this.#report(item as PendingItem);
        }
      }),
      shell.on('liveActivityEnded', (data) => {
        const mowerId = (data as { mowerId?: unknown } | null)?.mowerId;
        if (typeof mowerId !== 'string') return;
        void this.#installationId()
          .then(async (installation) => {
            if (installation) await this.#deps.api.endActivity(mowerId, installation);
          })
          .catch(() => undefined);
      }),
    );
  }
}
