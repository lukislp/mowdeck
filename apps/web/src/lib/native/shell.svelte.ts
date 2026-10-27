/**
 * The web app inside a MowDeck app shell (apps/mobile, ADR 0036): deep links, notification taps,
 * the app icon badge, local notifications from the inbox and, on iOS, the APNs registration.
 * In a browser `active` is false and every method returns at once, so the callers need no
 * checks of their own.
 *
 * Notifications per platform:
 * - iOS: APNs from the Notifications service (background, closed and foreground app); the inbox
 *   feeds local notifications only when APNs is denied or failed on this device (no duplicates,
 *   not while the registration is still pending).
 * - Android: no Firebase, so no push from the server. The inbox feeds local notifications while
 *   the app runs; in the background ntfy (its own app) delivers. With an ntfy target set up the
 *   local notifications stay off (ntfy shows the same message).
 */
import { ApnsRegistration, type ApnsApi, type ApnsState } from './apns';
import type { NativeBridge, NativePlatform } from './capacitor';
import {
  handoffAvailable,
  signInThroughBrowser,
  startAppLogin,
  type HandoffResult,
} from './handoff';
import { pathFromDeepLink, pathFromNotificationAction } from './links';
import { WidgetsRegistration, type WidgetsApi, type WidgetsState } from './widgets';
import { CHANNELS, InboxNotifier, toLocalNotification } from './local';
import type { InboxItem } from '#lib/stores/inbox.svelte.js';

export interface NativeShellDeps {
  bridge: NativeBridge | null;
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null;
  apns: ApnsApi;
  /** Widgets and the Live Activity (iOS shells of ADR 0059); without it they stay off. */
  widgetsApi?: WidgetsApi;
  /** True when the user has an ntfy target (Android background channel). */
  ntfyConfigured: () => Promise<boolean>;
}

type Display = 'granted' | 'denied' | 'prompt' | 'prompt-with-rationale';

export class NativeShell {
  /** APNs on this device (iOS). */
  apns = $state<ApnsState>('idle');
  /** Widgets and the Live Activity on this device (iOS, ADR 0059). */
  widgets = $state<WidgetsState>('unsupported');
  /** Local notifications allowed by the user (null: not asked yet). */
  local = $state<boolean | null>(null);
  /** The server origin the shell loads (from the shell's own plugin). */
  server = $state<string | null>(null);
  /** The user has an ntfy target (Android: then ntfy notifies, not the app). */
  ntfy = $state(false);

  readonly #deps: NativeShellDeps;
  readonly #notifier: InboxNotifier;
  readonly #registration: ApnsRegistration | null;
  readonly #widgets: WidgetsRegistration | null;
  #signedIn = false;

  constructor(deps: NativeShellDeps) {
    this.#deps = deps;
    this.#notifier = new InboxNotifier(deps.storage);
    const bridge = deps.bridge;
    this.#registration =
      bridge?.platform === 'ios' && bridge.has('PushNotifications')
        ? new ApnsRegistration({
            push: bridge.plugin('PushNotifications'),
            api: deps.apns,
            storage: deps.storage,
            deviceName: 'MowDeck iOS',
            installationId: () => this.#installationId(),
            onChange: (state) => (this.apns = state),
          })
        : null;
    this.#widgets =
      bridge?.platform === 'ios' &&
      deps.widgetsApi &&
      bridge.hasMethod('MowDeckShell', 'configureWidgets')
        ? new WidgetsRegistration({
            shell: bridge.plugin('MowDeckShell'),
            api: deps.widgetsApi,
            serverUrl: () => this.server,
            deviceName: 'MowDeck iOS',
            onChange: (state) => (this.widgets = state),
          })
        : null;
    if (this.#widgets) this.widgets = 'off';
  }

  get active(): boolean {
    return this.#deps.bridge !== null;
  }

  get platform(): NativePlatform | null {
    return this.#deps.bridge?.platform ?? null;
  }

  /** APNs is possible on this device (iOS shell with the push plugin). */
  get apnsAvailable(): boolean {
    return this.#registration !== null;
  }

  /** Widgets can be turned on here (iOS shell with the widget methods). */
  get widgetsAvailable(): boolean {
    return this.#widgets !== null;
  }

  #plugin(name: string) {
    const bridge = this.#deps.bridge;
    return bridge && bridge.has(name) ? bridge.plugin(name) : null;
  }

  /**
   * The installation id the iOS shell keeps in the Keychain (survives app updates and
   * reinstalls on this device); null for shells without `getInstallationId`.
   */
  async #installationId(): Promise<string | null> {
    const shell = this.#plugin('MowDeckShell');
    if (!shell) return null;
    const result = await shell.call<{ id?: unknown } | undefined>('getInstallationId');
    return typeof result?.id === 'string' ? result.id : null;
  }

  /**
   * Start once when the app mounts: deep links (`mowdeck://`), taps on local and push
   * notifications, the splash screen. Returns the cleanup.
   */
  start(navigate: (path: string) => void): () => void {
    if (!this.active) return () => undefined;
    const offs: (() => void)[] = [];
    const app = this.#plugin('App');
    if (app) {
      const open = (data: unknown) => {
        const url = (data as { url?: unknown } | null)?.url;
        const path = typeof url === 'string' ? pathFromDeepLink(url) : null;
        if (path) navigate(path);
      };
      offs.push(app.on('appUrlOpen', open));
      void app
        .call<{ url?: string } | undefined>('getLaunchUrl')
        .then((launch) => launch && open(launch))
        .catch(() => undefined);
    }
    const tapped = (action: unknown) => navigate(pathFromNotificationAction(action));
    const local = this.#plugin('LocalNotifications');
    if (local) offs.push(local.on('localNotificationActionPerformed', tapped));
    const push = this.#plugin('PushNotifications');
    if (push) offs.push(push.on('pushNotificationActionPerformed', tapped));
    void this.#plugin('SplashScreen')
      ?.call('hide')
      .catch(() => undefined);
    void this.#plugin('MowDeckShell')
      ?.call<{ url: string | null }>('getServerUrl')
      .then(({ url }) => (this.server = url))
      .catch(() => undefined);
    return () => {
      for (const off of offs) off();
      this.#registration?.stop();
      this.#widgets?.stop();
    };
  }

  /** After login (or a restored session): permissions, channels, APNs. */
  async signedIn(): Promise<void> {
    if (!this.active || this.#signedIn) return;
    this.#signedIn = true;
    if (this.platform === 'android') {
      this.ntfy = await this.#deps.ntfyConfigured().catch(() => false);
    }
    // iOS: the APNs prompt also grants local notifications, so it goes first.
    if (this.#registration) await this.#registration.enable();
    await this.#prepareLocal();
    await this.#widgets?.maintain();
  }

  /** Before logout: remove this device's APNs token from the server, clear the badge. */
  async signingOut(): Promise<void> {
    if (!this.active) return;
    this.#signedIn = false;
    await this.#registration?.disable();
    await this.#widgets?.disable().catch(() => undefined);
    await this.#plugin('Badge')
      ?.call('clear')
      .catch(() => undefined);
  }

  /** Ask for APNs again (the button on the settings page). */
  async enableApns(): Promise<void> {
    await this.#registration?.enable();
  }

  /** Turn the widgets and the Live Activity on (the button on the settings page). */
  async enableWidgets(): Promise<void> {
    await this.#widgets?.enable();
  }

  /** Turn them off: the credential is revoked and removed from this device. */
  async disableWidgets(): Promise<void> {
    await this.#widgets?.disable();
  }

  /** Re-read whether the user has an ntfy target (after it was saved or removed). */
  async refreshNtfy(): Promise<void> {
    if (this.platform === 'android') {
      this.ntfy = await this.#deps.ntfyConfigured().catch(() => false);
    }
  }

  /**
   * The shell can run a login in the system browser and hand it to the app (passkeys; app
   * builds with ADR 0050; older builds lack `MowDeckShell.openAuthSession`).
   */
  get canSignInThroughBrowser(): boolean {
    return handoffAvailable(this.#deps.bridge);
  }

  /** Sign in through the system browser (see `handoff.ts`); `redeem` starts the app session. */
  async signInThroughBrowser(
    origin: string,
    redeem: (code: string, verifier: string) => Promise<void>,
    signal?: AbortSignal,
  ): Promise<HandoffResult> {
    const bridge = this.#deps.bridge;
    if (!handoffAvailable(bridge)) return 'cancelled';
    return signInThroughBrowser({ bridge, origin, start: startAppLogin, redeem }, signal);
  }

  /** Forget the server: the shell shows its first-launch screen again. */
  async changeServer(): Promise<void> {
    await this.#plugin('MowDeckShell')?.call('resetServerUrl');
  }

  /** Every inbox refresh: badge, and local notifications for new items when wanted. */
  async inboxChanged(items: readonly InboxItem[], unread: number): Promise<void> {
    if (!this.active || !this.#signedIn) return;
    void this.#plugin('Badge')
      ?.call('set', { count: unread })
      .catch(() => undefined);
    const fresh = this.#notifier.fresh(items);
    if (!fresh.length || !this.#localWanted()) return;
    await this.#plugin('LocalNotifications')
      ?.call('schedule', { notifications: fresh.map(toLocalNotification) })
      .catch(() => undefined);
  }

  #localWanted(): boolean {
    if (this.local !== true) return false;
    // iOS: APNs shows the message (also in the foreground, presentationOptions). The inbox may
    // load while the registration is still under way ('pending', 'idle'), so only a failed or
    // refused APNs falls back to local notifications; otherwise an item the OS already showed
    // would appear a second time when the app opens.
    if (this.platform === 'ios') {
      return !this.#registration || this.apns === 'denied' || this.apns === 'error';
    }
    return !this.ntfy;
  }

  async #prepareLocal(): Promise<void> {
    const local = this.#plugin('LocalNotifications');
    if (!local) return;
    try {
      let { display } = await local.call<{ display: Display }>('checkPermissions');
      if (display === 'prompt' || display === 'prompt-with-rationale') {
        ({ display } = await local.call<{ display: Display }>('requestPermissions'));
      }
      this.local = display === 'granted' ? true : display === 'denied' ? false : null;
      if (this.platform === 'android') {
        for (const channel of CHANNELS) await local.call('createChannel', channel);
      }
    } catch {
      this.local = null;
    }
  }
}
