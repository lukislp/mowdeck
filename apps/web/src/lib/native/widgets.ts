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
 * The device remembers that it wants widgets (web storage of the shell). A sign-out ends the
 * credential and clears it in the shell with the stored mower states, so the widgets show no
 * data, but the wish stays: the next sign-in sets the widgets up again by itself. Only the
 * switch in the settings turns them off for good. The server ends a widget credential with the
 * login session that created it (ADR 0059), so none stays usable after a sign-out even when
 * the app could not revoke it (a session the security center had already ended, ADR 0065);
 * such a failed revocation is also retried after the next sign-in of the same user. Credentials
 * belong to their user: another user signing in on this device never uses or replaces them.
 * A shell that cannot report its state shows `error` (with a retry), never `off`.
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
  /**
   * Web storage of the shell: whether this device wants widgets, the credential it holds and
   * revocations still to do.
   */
  storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null;
  /**
   * The signed-in user (`<user>@<account>`). A credential belongs to the user who created it
   * and is never used, replaced or revoked on behalf of another one.
   */
  user?: () => string | null;
  /**
   * The login session of the app (its family id). The server ends a widget credential with the
   * session it was created in, so a credential from an earlier session is replaced.
   */
  session?: () => string | null;
  /** Delay before a failed start is tried again (tests shorten it). */
  retryMs?: number;
}

/** This device wants widgets: kept across sign-outs, cleared only by turning widgets off. */
export const WANTED_KEY = 'mowdeck.widgets.wanted';
/** The widget credential this device holds: `{ id, user }`. */
export const CREDENTIAL_KEY = 'mowdeck.widgets.credential';
/** Credentials of this device whose revocation failed, `[{ id, user }]`; retried by their user. */
export const REVOKE_KEY = 'mowdeck.widgets.revoke';
/** How often a failed start is tried again before the state stays `error`. */
const MAX_RETRIES = 3;

interface Held {
  id: string;
  /** Null for a credential recorded before the owner was. */
  user: string | null;
  /** The login session it was created in (null: unknown). */
  session?: string | null;
}

/** The HTTP status of an API error (`ApiProblem`), or null. */
function statusOf(error: unknown): number | null {
  const status = (error as { status?: unknown } | null)?.status;
  return typeof status === 'number' ? status : null;
}

function asHeld(value: unknown): Held | null {
  if (typeof value === 'string' && value) return { id: value, user: null };
  const item = value as { id?: unknown; user?: unknown } | null;
  if (!item || typeof item.id !== 'string') return null;
  const session = (value as { session?: unknown }).session;
  return {
    id: item.id,
    user: typeof item.user === 'string' ? item.user : null,
    session: typeof session === 'string' ? session : null,
  };
}

function parse(raw: string | null): unknown {
  if (raw === null) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
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

  #user(): string | null {
    return this.#deps.user?.() ?? null;
  }

  /** A credential of the signed-in user (one recorded without its owner counts as theirs). */
  #mine(held: Held): boolean {
    return held.user === null || held.user === this.#user();
  }

  get #wanted(): boolean {
    return this.#read(WANTED_KEY) === '1';
  }

  #setWanted(wanted: boolean): void {
    this.#write(WANTED_KEY, wanted ? '1' : null);
  }

  /** The credential this device holds on the server (as far as it knows). */
  #held(): Held | null {
    return asHeld(parse(this.#read(CREDENTIAL_KEY)));
  }

  #hold(id: string | null, session: string | null = this.#session()): void {
    this.#write(CREDENTIAL_KEY, id ? JSON.stringify({ id, user: this.#user(), session }) : null);
  }

  #session(): string | null {
    return this.#deps.session?.() ?? null;
  }

  #toRevoke(): Held[] {
    const list = parse(this.#read(REVOKE_KEY));
    return Array.isArray(list) ? list.map(asHeld).filter((h): h is Held => h !== null) : [];
  }

  #setToRevoke(items: Held[]): void {
    const unique = [...new Map(items.map((h) => [h.id, h])).values()];
    this.#write(REVOKE_KEY, unique.length ? JSON.stringify(unique) : null);
  }

  #queueRevocation(held: Held): void {
    this.#setToRevoke([...this.#toRevoke(), held]);
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

  /**
   * Retry the revocations of the signed-in user that failed before (a sign-out whose session
   * had already ended). Another user's stay until that user signs in here again; the server
   * ends them with their login session anyway.
   */
  async #flushRevocations(except: string | null = null): Promise<void> {
    const left: Held[] = [];
    for (const held of this.#toRevoke()) {
      if (held.id === except) continue;
      if (!this.#mine(held) || !(await this.#revoke(held.id))) left.push(held);
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
    this.#hold(created.id);
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

  /** The state of this device: are widgets set up? */
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
   * Turn widgets on: credential, mower list, device registration, and remember that this
   * device wants them. The signed-in user's credential this device already holds (in the shell,
   * or remembered here when the shell lost it) is replaced, so turning widgets on again never
   * leaves a second active credential behind; another user's is never touched.
   */
  async enable(): Promise<WidgetsState> {
    this.#cancelRetry();
    this.#set('pending');
    try {
      const server = this.#deps.serverUrl();
      if (!server) throw new Error('no server');
      const status = await this.#status();
      const held = this.#held();
      const mine = held === null || this.#mine(held);
      if (!mine && held) this.#queueRevocation(held);
      const replaces = mine ? (status.credentialId ?? held?.id ?? null) : null;
      await this.#flushRevocations(replaces);
      await this.#configure(server, await this.#create(replaces));
      this.#setWanted(true);
      this.#listen();
      await this.#register();
      this.#set('on');
    } catch {
      this.#set('error');
    }
    return this.#state;
  }

  /**
   * End this device's credential: forget the device on the server, revoke the credential (or
   * remember it for later when that fails, for instance at a sign-out whose session had already
   * ended) and clear it in the shell together with the stored mower states, so the widgets show
   * nothing until the next sign-in.
   */
  async #end(signedOut: boolean): Promise<void> {
    this.#cancelRetry();
    this.stop();
    const status = await this.#status().catch((): NativeStatus => ({}));
    const held = this.#held();
    const id = status.credentialId ?? held?.id ?? null;
    const installation = await this.#installationId().catch(() => null);
    if (installation) await this.#deps.api.unregisterDevice(installation).catch(() => undefined);
    if (id && !(await this.#revoke(id))) {
      this.#queueRevocation({ id, user: held && held.id === id ? held.user : this.#user() });
    }
    this.#hold(null);
    await this.#deps.shell.call('clearWidgets', { signedOut });
  }

  /** Turn widgets off for good on this device (the switch in the settings). */
  async disable(): Promise<void> {
    try {
      this.#setWanted(false);
      await this.#end(false);
    } finally {
      this.#set('off');
    }
  }

  /**
   * Before a sign-out: the credential ends and the widgets show no data, but the device still
   * wants widgets, so the next sign-in sets them up again by itself.
   */
  async signOut(): Promise<void> {
    try {
      await this.#end(true);
    } finally {
      this.#set('pending');
    }
  }

  /**
   * On every start of a signed-in app: retry revocations that failed; a credential of another
   * user is ended; a device that wants widgets but has no credential gets one; with widgets on
   * replace a credential that expires within 30 days, refresh the mower list, register the
   * device and hand over the Live Activity tokens the shell collected while the web app was not
   * running. A shell that cannot report its state is `error` and asked again shortly, never
   * `off`.
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
    const held = this.#held();
    if (status.configured && held && !this.#mine(held)) {
      // left behind by another user whose session ended without a sign-out here
      this.#queueRevocation(held);
      this.#hold(null);
      await this.#deps.shell.call('clearWidgets', { signedOut: true }).catch(() => undefined);
      status = { ...status, configured: false, credentialId: null };
    }
    await this.#flushRevocations(status.credentialId ?? null);
    if (!status.configured) {
      if (this.#wanted || (held !== null && this.#mine(held))) {
        if ((await this.enable()) === 'error') this.#scheduleRetry();
      } else {
        this.#set('off');
      }
      return;
    }
    // A credential of an earlier login session stopped working when that session ended (the
    // security center, a session that expired): replace it with one of this session.
    const session = this.#session();
    const known = this.#held();
    const earlierSession =
      session !== null &&
      (known === null || known.id !== status.credentialId || known.session !== session);
    if (status.credentialId) {
      this.#hold(
        status.credentialId,
        known?.id === status.credentialId ? (known.session ?? null) : null,
      );
    }
    this.#setWanted(true);
    this.#set('on');
    this.#listen();
    try {
      if (status.needsRotation || earlierSession) {
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
