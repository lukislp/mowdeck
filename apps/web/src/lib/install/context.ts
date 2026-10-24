/**
 * Where the web app runs, as far as "install MowDeck" is concerned:
 *
 * - `native-shell`: inside the iOS or Android app shell (apps/mobile, ADR 0036). The app is
 *   installed natively; no install hint, no install button, no service worker update banner.
 * - `installed`: an installed web app (home screen on iPhone and iPad, Chrome or Edge app on
 *   Windows, macOS and Android), told by the display mode or Safari's `navigator.standalone`.
 * - `ios-browser`: a browser tab on iPhone or iPad. Safari has no install prompt, so the page
 *   explains the share sheet ("Add to Home Screen"). iPadOS reports a desktop "Macintosh" user
 *   agent; touch points tell it from a Mac.
 * - `browser`: any other browser tab. It offers installing only when the browser fires
 *   `beforeinstallprompt` (Chrome and Edge, not for an app that is already installed).
 *
 * The shell is recognised by the injected Capacitor bridge or by the `MowDeckApp` token the
 * shells append to their user agent (`apps/mobile/capacitor.config.ts`); either is enough.
 */

export type InstallContext = 'native-shell' | 'installed' | 'ios-browser' | 'browser';

/** The token `appendUserAgent` adds to the shells' user agent. */
export const SHELL_UA_TOKEN = 'MowDeckApp';

/** Display modes of an installed web app (the manifest asks for `standalone`). */
export const INSTALLED_DISPLAY_MODES = [
  'standalone',
  'fullscreen',
  'minimal-ui',
  'window-controls-overlay',
] as const;

export interface InstallEnvironment {
  userAgent: string;
  maxTouchPoints: number;
  /** True when `(display-mode: <mode>)` matches. */
  displayMode: (mode: string) => boolean;
  /** Safari's `navigator.standalone` (a home screen web app on iPhone and iPad). */
  navigatorStandalone: boolean;
  /** The Capacitor bridge says it runs on a native platform. */
  nativeBridge: boolean;
}

export function isShellUserAgent(userAgent: string): boolean {
  return new RegExp(`\\b${SHELL_UA_TOKEN}\\b`).test(userAgent);
}

/** iPhone, iPod or iPad, including an iPad that reports the desktop "Macintosh" user agent. */
export function isAppleMobile(userAgent: string, maxTouchPoints: number): boolean {
  return (
    /iphone|ipad|ipod/i.test(userAgent) || (userAgent.includes('Macintosh') && maxTouchPoints > 1)
  );
}

export function isInstalledDisplay(
  env: Pick<InstallEnvironment, 'displayMode' | 'navigatorStandalone'>,
): boolean {
  return env.navigatorStandalone || INSTALLED_DISPLAY_MODES.some((mode) => env.displayMode(mode));
}

export function installContext(env: InstallEnvironment): InstallContext {
  if (env.nativeBridge || isShellUserAgent(env.userAgent)) return 'native-shell';
  if (isInstalledDisplay(env)) return 'installed';
  if (isAppleMobile(env.userAgent, env.maxTouchPoints)) return 'ios-browser';
  return 'browser';
}

/** The environment of `scope` (the window); an empty one outside a browser. */
export function readInstallEnvironment(scope: unknown = globalThis): InstallEnvironment {
  const w = scope as
    | (Partial<Pick<Window, 'matchMedia' | 'navigator'>> & {
        Capacitor?: { isNativePlatform?: () => boolean };
      })
    | undefined;
  const nav = w?.navigator as (Navigator & { standalone?: boolean }) | undefined;
  let nativeBridge: boolean;
  try {
    nativeBridge = w?.Capacitor?.isNativePlatform?.() === true;
  } catch {
    nativeBridge = false;
  }
  return {
    userAgent: nav?.userAgent ?? '',
    maxTouchPoints: nav?.maxTouchPoints ?? 0,
    displayMode: (mode) => {
      try {
        return w?.matchMedia?.(`(display-mode: ${mode})`).matches ?? false;
      } catch {
        return false;
      }
    },
    navigatorStandalone: nav?.standalone === true,
    nativeBridge,
  };
}
