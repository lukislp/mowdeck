/**
 * Small per-tab UI state: toasts, theme, install offer, service worker update. Only the theme
 * choice and the install hint flags are kept in localStorage (`mowdeck.theme`,
 * `mowdeck.install-hint-dismissed`, `mowdeck.installed`), never anything about the session.
 */
import type { ToastItem, ToastTone } from '@mowdeck/ui';

import {
  installContext,
  readInstallEnvironment,
  type InstallContext,
} from '#lib/install/context.js';

// --- toasts -------------------------------------------------------------------------------------

class Toasts {
  items = $state<ToastItem[]>([]);
  #counter = 0;

  push(tone: ToastTone, title: string, detail?: string, code?: string): string {
    this.#counter += 1;
    const id = `t${this.#counter}`;
    this.items = [...this.items.slice(-3), { id, tone, title, detail, code }];
    setTimeout(() => this.dismiss(id), tone === 'error' ? 10_000 : 5_000);
    return id;
  }

  dismiss(id: string): void {
    this.items = this.items.filter((t) => t.id !== id);
  }
}

export const toasts = new Toasts();

// --- theme --------------------------------------------------------------------------------------

export type ThemeChoice = 'system' | 'light' | 'dark';
const THEME_KEY = 'mowdeck.theme';

function readTheme(): ThemeChoice {
  try {
    const stored = typeof localStorage !== 'undefined' ? localStorage.getItem(THEME_KEY) : null;
    return stored === 'light' || stored === 'dark' ? stored : 'system';
  } catch {
    return 'system';
  }
}

class Theme {
  choice = $state<ThemeChoice>(readTheme());

  set(choice: ThemeChoice): void {
    this.choice = choice;
    try {
      if (choice === 'system') localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, choice);
    } catch {
      // storage blocked: the choice lasts for this page only
    }
    applyTheme(choice);
  }
}

/** Mirrors static/theme-init.js (which runs before the first paint). */
export function applyTheme(choice: ThemeChoice): void {
  if (typeof document === 'undefined') return;
  if (choice === 'system') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = choice;
}

export const theme = new Theme();

// --- install offer -------------------------------------------------------------------------------

interface InstallPromptEvent extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

interface RelatedAppsNavigator {
  getInstalledRelatedApps?: () => Promise<unknown[]>;
}

const HINT_KEY = 'mowdeck.install-hint-dismissed';
/** Set on `appinstalled`, cleared when the browser offers installing again. */
const INSTALLED_KEY = 'mowdeck.installed';

/** What the page offers: the browser's install prompt, the iOS share sheet text, or nothing. */
export type InstallOffer = 'prompt' | 'ios' | null;

function readFlag(key: string): boolean {
  try {
    return localStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

function writeFlag(key: string, on: boolean): void {
  try {
    if (on) localStorage.setItem(key, '1');
    else localStorage.removeItem(key);
  } catch {
    // storage blocked: lasts for this page only
  }
}

class Install {
  /** Where the app runs (`src/lib/install/context.ts`). */
  context = $state<InstallContext>('browser');
  /** The browser fired `beforeinstallprompt` (Chrome, Edge): installing is possible here. */
  available = $state(false);
  /** This browser installed the web app (remembered `appinstalled`, or related apps). */
  installedElsewhere = $state(false);
  hintDismissed = $state(false);
  #event: InstallPromptEvent | null = null;

  /** The app runs inside the native shell. */
  get native(): boolean {
    return this.context === 'native-shell';
  }

  /** The installed web app is running (not a tab). */
  get standalone(): boolean {
    return this.context === 'installed';
  }

  get offer(): InstallOffer {
    if (this.context === 'native-shell' || this.context === 'installed') return null;
    if (this.available) return 'prompt';
    if (this.context === 'ios-browser' && !this.installedElsewhere) return 'ios';
    return null;
  }

  init(target?: Window): void {
    const scope = target ?? (typeof window === 'undefined' ? undefined : window);
    if (!scope) return;
    this.context = installContext(readInstallEnvironment(scope));
    this.hintDismissed = readFlag(HINT_KEY);
    this.installedElsewhere = readFlag(INSTALLED_KEY);
    scope.addEventListener('beforeinstallprompt', (event) => {
      event.preventDefault();
      if (this.context !== 'browser' && this.context !== 'ios-browser') return;
      this.#event = event as InstallPromptEvent;
      this.available = true;
      // Chromium offers installing only while the app is not installed: forget an old install.
      this.installedElsewhere = false;
      writeFlag(INSTALLED_KEY, false);
    });
    scope.addEventListener('appinstalled', () => {
      this.#event = null;
      this.available = false;
      this.installedElsewhere = true;
      writeFlag(INSTALLED_KEY, true);
    });
    // Chromium answers for the manifest's `related_applications` (the web app itself).
    const related = (scope.navigator as RelatedAppsNavigator | undefined)?.getInstalledRelatedApps;
    if (this.context === 'browser' && typeof related === 'function') {
      void related
        .call(scope.navigator)
        .then((apps) => {
          if (apps.length > 0) this.installedElsewhere = true;
        })
        .catch(() => undefined);
    }
  }

  async prompt(): Promise<void> {
    const event = this.#event;
    if (!event) return;
    await event.prompt();
    await event.userChoice;
    this.#event = null;
    this.available = false;
  }

  dismissHint(): void {
    this.hintDismissed = true;
    writeFlag(HINT_KEY, true);
  }
}

export const install = new Install();

// --- service worker update ------------------------------------------------------------------------

class AppUpdate {
  waiting = $state<ServiceWorker | null>(null);
  #applying = false;

  /**
   * Register the service worker. Inside the native app shell (`native`) there is none: the
   * shell loads every server release as it is (ADR 0036), so a worker left from an earlier
   * version is removed and no update banner appears.
   */
  async register(native = false): Promise<void> {
    if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
    if (native) {
      try {
        const registrations = await navigator.serviceWorker.getRegistrations();
        await Promise.all(registrations.map((registration) => registration.unregister()));
      } catch {
        // nothing registered, or the WebView has no service workers
      }
      return;
    }
    if (!import.meta.env.PROD) return;
    try {
      const registration = await navigator.serviceWorker.register('/service-worker.js', {
        type: 'module',
      });
      const watch = (worker: ServiceWorker | null) => {
        worker?.addEventListener('statechange', () => {
          if (worker.state === 'installed' && navigator.serviceWorker.controller) {
            this.waiting = worker;
          }
        });
      };
      if (registration.waiting && navigator.serviceWorker.controller) {
        this.waiting = registration.waiting;
      }
      registration.addEventListener('updatefound', () => watch(registration.installing));
      // Reload only for an update the user asked for; the first install also changes the
      // controller (clients.claim) and must not reload a page the user is typing in.
      navigator.serviceWorker.addEventListener('controllerchange', () => {
        if (!this.#applying) return;
        this.#applying = false;
        window.location.reload();
      });
    } catch {
      // no service worker (private mode, HTTP on another host): the app works without it
    }
  }

  apply(): void {
    this.#applying = true;
    this.waiting?.postMessage({ type: 'skip-waiting' });
  }
}

export const appUpdate = new AppUpdate();
