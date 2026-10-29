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
 * The device remembers the id of the credential it was given (web storage of the shell), so a
 * credential is never left active on the server without a way back to it: turning widgets on
 * again replaces it even when the shell's Keychain item is gone, and a revocation that failed
 * (for instance at a sign-out whose session the security center had already ended, ADR 0065)
 * is retried after the next sign-in. A shell that cannot report its state, or that lost a
 * credential this device still holds on the server, shows `error` (with a retry), never `off`.
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
  /** Web storage of the shell: the id of this device's credential, revocations still to do. */
  storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null;
  /** Delay before a failed start is tried again (tests shorten it). */
  retryMs?: number;
}

/** The id of the widget credential this device was last given. */
export const CREDENTIAL_KEY = 'mowdeck.widgets.credential';
/** Ids of credentials of this device whose revocation failed; retried after sign-in. */
export const REVOKE_KEY = 'mowdeck.widgets.revoke';
/** How often a failed start is tried again before the state stays `error`. */
const MAX_RETRIES = 3;

/** The HTTP status of an API error (`ApiProblem`), or null. */
function statusOf(error: unknown): number | null {
  const status = (error as { status?: unknown } | null)?.status;
  return typeof status === 'number' ? status : null;
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
  #retry: ReturnType<typeof setTimeout> | null = null;
  #retries = 0;

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

  // --- what this device remembers (web storage; blocked storage just remembers nothing) ------

  #read(key: string): string | null {
    try {
      return this.#deps.storage?.getItem(key) ?? null;
    } catch {
      return null;
    }
  }

  #write(key: string, value: string | null): void {
    try {
      if (value === null) this.#deps.storage?.removeItem(key);
      else this.#deps.storage?.setItem(key, value);
    } catch {
      // storage blocked: nothing to remember
    }
  }

  /** The credential this device holds on the server (as far as it knows). */
  #known(): string | null {
    return this.#read(CREDENTIAL_KEY);
  }

  #toRevoke(): string[] {
    try {
      const list: unknown = JSON.parse(this.#read(REVOKE_KEY) ?? '[]');
      return Array.isArray(list) ? list.filter((id): id is string => typeof id === 'string') : [];
    } catch {
      return [];
    }
  }

  #setToRevoke(ids: string[]): void {
    this.#write(REVOKE_KEY, ids.length ? JSON.stringify([...new Set(ids)]) : null);
  }

  /** Revoke a credential; true when it is gone (revoked now, or unknown to the server). */
  async #revoke(id: string): Promise<boolean> {
    try {
      await this.#deps.api.revokeCredential(id);
      return true;
    } catch (error) {
      return statusOf(error) === 404;
    }
  }

  /** Retry the revocations that failed before (a sign-out with an ended session). */
  async #flushRevocations(except: string | null = null): Promise<void> {
    const left: string[] = [];
    for (const id of this.#toRevoke()) {
      if (id === except) continue;
      if (!(await this.#revoke(id))) left.push(id);
    }
    this.#setToRevoke(left);
  }

  /** A credential from the server, with `replaces` revoked; without it when that is gone. */
  async #create(replaces: string | null): Promise<CreatedCredential> {
    try {
      return await this.#deps.api.createCredential(this.#deps.deviceName, replaces);
    } catch (error) {
      if (replaces === null || statusOf(error) !== 404) throw error;
      return this.#deps.api.createCredential(this.#deps.deviceName, null);
    }
  }

  async #configure(server: string, created: CreatedCredential): Promise<void> {
    await this.#deps.shell.call('configureWidgets', {
      serverUrl: server,
      secret: created.token,
      credentialId: created.id,
      expiresAt: wholeSeconds(created.expires_at),
      deviceName: this.#deps.deviceName,
      mowers: await this.#deps.api.mowers().catch(() => []),
    });
    this.#write(CREDENTIAL_KEY, created.id);
  }

  #scheduleRetry(): void {
    if (this.#retry || this.#retries >= MAX_RETRIES) return;
    this.#retries += 1;
    this.#retry = setTimeout(() => {
      this.#retry = null;
      void this.maintain();
    }, this.#deps.retryMs ?? 15_000);
  }

  #cancelRetry(): void {
    if (this.#retry) clearTimeout(this.#retry);
    this.#retry = null;
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

  /**
   * Turn widgets on: credential, mower list, device registration. The credential this device
   * already holds (in the shell, or remembered here when the shell lost it) is replaced, so
   * turning widgets on again never leaves a second active credential behind.
   */
  async enable(): Promise<WidgetsState> {
    this.#cancelRetry();
    this.#set('pending');
    try {
      const server = this.#deps.serverUrl();
      if (!server) throw new Error('no server');
      const status = await this.#status();
      const replaces = status.credentialId ?? this.#known();
      await this.#flushRevocations(replaces);
      await this.#configure(server, await this.#create(replaces));
      this.#listen();
      await this.#register();
      this.#set('on');
    } catch {
      this.#set('error');
    }
    return this.#state;
  }

  /**
   * Turn widgets off: forget the device on the server, revoke the credential, clear it. A
   * revocation that fails (offline, or a sign-out whose session already ended) is remembered
   * and retried after the next sign-in, so the credential never stays active unnoticed.
   */
  async disable(): Promise<void> {
    this.#cancelRetry();
    try {
      const status = await this.#status().catch((): NativeStatus => ({}));
      const id = status.credentialId ?? this.#known();
      const installation = await this.#installationId().catch(() => null);
      if (installation) await this.#deps.api.unregisterDevice(installation).catch(() => undefined);
      if (id && !(await this.#revoke(id))) this.#setToRevoke([...this.#toRevoke(), id]);
      this.#write(CREDENTIAL_KEY, null);
      await this.#deps.shell.call('clearWidgets');
    } finally {
      this.#set('off');
    }
  }

  /**
   * On every start of a signed-in app: retry revocations that failed, and with widgets on
   * replace a credential that expires within 30 days, refresh the mower list, register the
   * device and hand over the Live Activity tokens the shell collected while the web app was not
   * running. A shell that cannot report its state is `error` and asked again shortly; a shell
   * without the credential this device still holds on the server is `error` too (turning
   * widgets on replaces it), never `off`.
   */
  async maintain(): Promise<void> {
    let status: NativeStatus;
    try {
      status = await this.#status();
    } catch {
      this.#set('error');
      this.#scheduleRetry();
      return;
    }
    this.#retries = 0;
    if (status.supported === false) {
      this.#set('unsupported');
      return;
    }
    await this.#flushRevocations(status.credentialId ?? null);
    if (!status.configured) {
      this.#set(this.#known() ? 'error' : 'off');
      return;
    }
    if (status.credentialId) this.#write(CREDENTIAL_KEY, status.credentialId);
    this.#set('on');
    this.#listen();
    try {
      if (status.needsRotation) {
        const server = this.#deps.serverUrl();
        if (server) await this.#configure(server, await this.#create(status.credentialId ?? null));
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
    this.#cancelRetry();
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
