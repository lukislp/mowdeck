import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { install } from '#lib/stores/ui.svelte.js';

import {
  installContext,
  isAppleMobile,
  isShellUserAgent,
  readInstallEnvironment,
  type InstallEnvironment,
} from './context';

// Real user agents (the shells append `MowDeckApp`, apps/mobile/capacitor.config.ts).
const UA = {
  iphoneSafari:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1',
  // iPadOS Safari asks for the desktop site by default: a Mac user agent with touch points.
  ipadSafari:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15',
  macSafari:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15',
  iosShell:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MowDeckApp',
  ipadShell:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) MowDeckApp',
  androidShell:
    'Mozilla/5.0 (Linux; Android 15; Pixel 8 Build/AP4A; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/141.0.0.0 Mobile Safari/537.36 MowDeckApp',
  androidChrome:
    'Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36',
  windowsEdge:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0',
};

function env(
  overrides: Partial<InstallEnvironment> & { modes?: string[] } = {},
): InstallEnvironment {
  const { modes = [], ...rest } = overrides;
  return {
    userAgent: UA.windowsEdge,
    maxTouchPoints: 0,
    displayMode: (mode) => modes.includes(mode),
    navigatorStandalone: false,
    nativeBridge: false,
    ...rest,
  };
}

describe('installContext', () => {
  it('iPhone Safari: share sheet hint', () => {
    expect(installContext(env({ userAgent: UA.iphoneSafari, maxTouchPoints: 5 }))).toBe(
      'ios-browser',
    );
  });

  it('iPad Safari with the desktop user agent and touch: share sheet hint', () => {
    expect(installContext(env({ userAgent: UA.ipadSafari, maxTouchPoints: 5 }))).toBe(
      'ios-browser',
    );
  });

  it('a Mac (same user agent, no touch) is a plain browser', () => {
    expect(installContext(env({ userAgent: UA.macSafari, maxTouchPoints: 0 }))).toBe('browser');
  });

  it('home screen web app on iPhone and iPad (navigator.standalone or display-mode)', () => {
    expect(
      installContext(
        env({ userAgent: UA.iphoneSafari, maxTouchPoints: 5, navigatorStandalone: true }),
      ),
    ).toBe('installed');
    expect(
      installContext(env({ userAgent: UA.ipadSafari, maxTouchPoints: 5, modes: ['standalone'] })),
    ).toBe('installed');
  });

  it.each(['standalone', 'minimal-ui', 'window-controls-overlay', 'fullscreen'])(
    'installed Chrome/Edge app on Windows (display-mode: %s)',
    (mode) => {
      expect(installContext(env({ userAgent: UA.windowsEdge, modes: [mode] }))).toBe('installed');
    },
  );

  it('a Windows or Android browser tab is a plain browser (offer via beforeinstallprompt)', () => {
    expect(installContext(env({ userAgent: UA.windowsEdge }))).toBe('browser');
    expect(installContext(env({ userAgent: UA.androidChrome, maxTouchPoints: 5 }))).toBe('browser');
  });

  it('the iOS shell user agent is the native shell, also on iPad and without the bridge', () => {
    expect(installContext(env({ userAgent: UA.iosShell, maxTouchPoints: 5 }))).toBe('native-shell');
    expect(installContext(env({ userAgent: UA.ipadShell, maxTouchPoints: 5 }))).toBe(
      'native-shell',
    );
  });

  it('the Android shell user agent is the native shell', () => {
    expect(installContext(env({ userAgent: UA.androidShell, maxTouchPoints: 5 }))).toBe(
      'native-shell',
    );
  });

  it('the Capacitor bridge alone also means the native shell', () => {
    expect(installContext(env({ userAgent: UA.iphoneSafari, nativeBridge: true }))).toBe(
      'native-shell',
    );
  });

  it('the shell wins over a standalone display mode', () => {
    expect(installContext(env({ userAgent: UA.iosShell, navigatorStandalone: true }))).toBe(
      'native-shell',
    );
  });
});

describe('helpers', () => {
  it('matches the shell token as a word only', () => {
    expect(isShellUserAgent(UA.androidShell)).toBe(true);
    expect(isShellUserAgent(UA.androidChrome)).toBe(false);
    expect(isShellUserAgent('Something MowDeckAppX')).toBe(false);
  });

  it('tells iPhone, iPad and Mac apart', () => {
    expect(isAppleMobile(UA.iphoneSafari, 0)).toBe(true);
    expect(isAppleMobile(UA.ipadSafari, 5)).toBe(true);
    expect(isAppleMobile(UA.macSafari, 0)).toBe(false);
  });

  it('reads the window', () => {
    const read = readInstallEnvironment(
      fakeWindow({ userAgent: UA.iphoneSafari, maxTouchPoints: 5, standalone: true }),
    );
    expect(read.userAgent).toBe(UA.iphoneSafari);
    expect(read.maxTouchPoints).toBe(5);
    expect(read.navigatorStandalone).toBe(true);
    expect(read.nativeBridge).toBe(false);
    expect(readInstallEnvironment(fakeWindow({ capacitor: true })).nativeBridge).toBe(true);
    expect(readInstallEnvironment(undefined).userAgent).toBe(navigator.userAgent);
  });
});

// --- the store ---------------------------------------------------------------------------------

interface FakeOptions {
  userAgent?: string;
  maxTouchPoints?: number;
  modes?: string[];
  standalone?: boolean;
  capacitor?: boolean;
  relatedApps?: unknown[];
}

function fakeWindow(options: FakeOptions = {}): Window {
  const target = new EventTarget();
  const navigator: Record<string, unknown> = {
    userAgent: options.userAgent ?? UA.windowsEdge,
    maxTouchPoints: options.maxTouchPoints ?? 0,
    standalone: options.standalone,
  };
  if (options.relatedApps) {
    const apps = options.relatedApps;
    navigator.getInstalledRelatedApps = () => Promise.resolve(apps);
  }
  return Object.assign(target, {
    navigator,
    matchMedia: (query: string) => ({
      matches: (options.modes ?? []).some((mode) => query === `(display-mode: ${mode})`),
    }),
    Capacitor: options.capacitor
      ? { isNativePlatform: () => true, getPlatform: () => 'ios' }
      : undefined,
  }) as unknown as Window;
}

function installPrompt(): Event {
  return Object.assign(new Event('beforeinstallprompt', { cancelable: true }), {
    prompt: () => Promise.resolve(),
    userChoice: Promise.resolve({ outcome: 'accepted' as const }),
  });
}

describe('install offer', () => {
  beforeEach(() => {
    localStorage.clear();
    install.available = false;
    install.installedElsewhere = false;
  });
  afterEach(() => localStorage.clear());

  it('iPhone Safari offers the share sheet text', () => {
    install.init(fakeWindow({ userAgent: UA.iphoneSafari, maxTouchPoints: 5 }));
    expect(install.offer).toBe('ios');
  });

  it('iPad Safari (desktop user agent) offers the share sheet text', () => {
    install.init(fakeWindow({ userAgent: UA.ipadSafari, maxTouchPoints: 5 }));
    expect(install.offer).toBe('ios');
  });

  it('the installed web app on iPhone and iPad offers nothing', () => {
    install.init(fakeWindow({ userAgent: UA.iphoneSafari, maxTouchPoints: 5, standalone: true }));
    expect(install.offer).toBeNull();
    expect(install.standalone).toBe(true);
    install.init(
      fakeWindow({ userAgent: UA.ipadSafari, maxTouchPoints: 5, modes: ['standalone'] }),
    );
    expect(install.offer).toBeNull();
  });

  it('the iOS shell offers nothing, also on iPad', () => {
    install.init(fakeWindow({ userAgent: UA.iosShell, maxTouchPoints: 5 }));
    expect(install.offer).toBeNull();
    expect(install.native).toBe(true);
    install.init(fakeWindow({ userAgent: UA.ipadShell, maxTouchPoints: 5 }));
    expect(install.offer).toBeNull();
  });

  it('the Android shell ignores beforeinstallprompt', () => {
    const w = fakeWindow({ userAgent: UA.androidShell, maxTouchPoints: 5 });
    install.init(w);
    const event = installPrompt();
    w.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(install.available).toBe(false);
    expect(install.offer).toBeNull();
  });

  it('a Windows browser tab offers nothing until beforeinstallprompt, then the prompt', async () => {
    const w = fakeWindow({ userAgent: UA.windowsEdge });
    install.init(w);
    expect(install.offer).toBeNull();
    w.dispatchEvent(installPrompt());
    expect(install.offer).toBe('prompt');
    await install.prompt();
    expect(install.offer).toBeNull();
  });

  it('the installed Windows app (window-controls-overlay) offers nothing, even on beforeinstallprompt', () => {
    const w = fakeWindow({ userAgent: UA.windowsEdge, modes: ['window-controls-overlay'] });
    install.init(w);
    w.dispatchEvent(installPrompt());
    expect(install.offer).toBeNull();
    expect(install.standalone).toBe(true);
  });

  it('remembers appinstalled for the tab and forgets it when the browser offers again', () => {
    const w = fakeWindow({ userAgent: UA.windowsEdge });
    install.init(w);
    w.dispatchEvent(installPrompt());
    w.dispatchEvent(new Event('appinstalled'));
    expect(install.offer).toBeNull();
    expect(localStorage.getItem('mowdeck.installed')).toBe('1');

    install.init(fakeWindow({ userAgent: UA.iphoneSafari, maxTouchPoints: 5 }));
    expect(install.installedElsewhere).toBe(true);
    expect(install.offer).toBeNull();

    const again = fakeWindow({ userAgent: UA.windowsEdge });
    install.init(again);
    again.dispatchEvent(installPrompt());
    expect(install.offer).toBe('prompt');
    expect(localStorage.getItem('mowdeck.installed')).toBeNull();
  });

  it('getInstalledRelatedApps marks the app installed in a browser tab', async () => {
    install.init(fakeWindow({ userAgent: UA.windowsEdge, relatedApps: [{ platform: 'webapp' }] }));
    await Promise.resolve();
    await Promise.resolve();
    expect(install.installedElsewhere).toBe(true);
    expect(install.offer).toBeNull();
  });

  it('dismissing the hint is remembered', () => {
    install.init(fakeWindow({ userAgent: UA.iphoneSafari, maxTouchPoints: 5 }));
    install.dismissHint();
    expect(localStorage.getItem('mowdeck.install-hint-dismissed')).toBe('1');
    install.init(fakeWindow({ userAgent: UA.iphoneSafari, maxTouchPoints: 5 }));
    expect(install.hintDismissed).toBe(true);
  });
});
