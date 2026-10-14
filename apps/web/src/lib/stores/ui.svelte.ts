/**
 * Small per-tab UI state: toasts, theme, install prompt, service worker update. Only the theme
 * choice is kept in localStorage (`mowdeck.theme`), never anything about the session.
 */
import type { ToastItem, ToastTone } from '@mowdeck/ui';

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

// --- install prompt (Chromium) and standalone detection ----------------------------------------

interface InstallPromptEvent extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

const HINT_KEY = 'mowdeck.install-hint-dismissed';

class Install {
  available = $state(false);
  standalone = $state(false);
  ios = $state(false);
  hintDismissed = $state(false);
  #event: InstallPromptEvent | null = null;

  init(): void {
    if (typeof window === 'undefined') return;
    this.standalone = window.matchMedia?.('(display-mode: standalone)').matches ?? false;
    this.ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
    try {
      this.hintDismissed = localStorage.getItem(HINT_KEY) === '1';
    } catch {
      this.hintDismissed = false;
    }
    window.addEventListener('beforeinstallprompt', (event) => {
      event.preventDefault();
      this.#event = event as InstallPromptEvent;
      this.available = true;
    });
    window.addEventListener('appinstalled', () => {
      this.available = false;
      this.standalone = true;
    });
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
    try {
      localStorage.setItem(HINT_KEY, '1');
    } catch {
      // ignore
    }
  }
}

export const install = new Install();

// --- service worker update ------------------------------------------------------------------------

class AppUpdate {
  waiting = $state<ServiceWorker | null>(null);
  #applying = false;

  async register(): Promise<void> {
    if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
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
