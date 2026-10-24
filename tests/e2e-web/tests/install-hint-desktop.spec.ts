/**
 * The install hint on the mower list and the "App" panel of the settings, per platform
 * (src/lib/install/context.ts): iPhone and iPad Safari get the share sheet text, a Chrome or Edge
 * tab gets the install button only after `beforeinstallprompt`, and nothing appears inside the
 * iOS or Android app shell (user agent token `MowDeckApp`) or in an installed web app (display
 * mode standalone, minimal-ui or window-controls-overlay, Safari's `navigator.standalone`).
 *
 * One login; every case opens its own tab in the same browser context (the session restores
 * from the refresh cookie) with the user agent and touch points set over CDP and the display
 * mode emulated by an init script. Chromium only (project desktop).
 */
import { expect, test, type BrowserContext, type Page } from '@playwright/test';

import { actor, loginByUi, registerByApi } from './support';

test.describe.configure({ mode: 'serial' });

const UA = {
  iphoneSafari:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1',
  ipadSafari:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15',
  iosShell:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MowDeckApp',
  ipadShell:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) MowDeckApp',
  androidShell:
    'Mozilla/5.0 (Linux; Android 15; Pixel 8 Build/AP4A; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/141.0.0.0 Mobile Safari/537.36 MowDeckApp',
  windowsEdge:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0',
};

const IOS_TEXT = 'Zum Home-Bildschirm';
const PROMPT_TEXT = 'MowDeck als App auf diesem Gerät installieren.';

interface Platform {
  userAgent: string;
  touchPoints?: number;
  displayMode?: 'standalone' | 'minimal-ui' | 'window-controls-overlay';
  navigatorStandalone?: boolean;
}

const user = actor('installer');
let context: BrowserContext;

test.beforeAll(async ({ browser, request }) => {
  await registerByApi(request, user);
  context = await browser.newContext({ locale: 'de-DE', viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  await loginByUi(page, user);
  await page.close();
});

test.afterAll(async () => {
  await context?.close();
});

async function open(platform: Platform, path = '/mowers'): Promise<Page> {
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send('Emulation.setUserAgentOverride', { userAgent: platform.userAgent });
  if (platform.touchPoints) {
    await cdp.send('Emulation.setTouchEmulationEnabled', {
      enabled: true,
      maxTouchPoints: platform.touchPoints,
    });
  }
  await page.addInitScript(
    ({ mode, standalone }) => {
      if (mode) {
        const original = window.matchMedia.bind(window);
        window.matchMedia = (query: string) => {
          const asked = /^\(display-mode:\s*([\w-]+)\)$/.exec(query.trim());
          if (!asked) return original(query);
          // A real MediaQueryList for the other methods, with the emulated answer.
          const list = original(query);
          return Object.defineProperty(list, 'matches', { value: asked[1] === mode });
        };
      }
      if (standalone) {
        Object.defineProperty(Navigator.prototype, 'standalone', { get: () => true });
      }
    },
    { mode: platform.displayMode ?? null, standalone: platform.navigatorStandalone ?? false },
  );
  // The hint is remembered as dismissed or installed per browser: start each case clean.
  await page.goto(path);
  await page.evaluate(() => {
    localStorage.removeItem('mowdeck.install-hint-dismissed');
    localStorage.removeItem('mowdeck.installed');
  });
  await page.reload();
  await ready(page, path);
  return page;
}

/** The page is rendered: the mower list's heading, or the profile section of the settings. */
async function ready(page: Page, path: string): Promise<void> {
  if (path === '/settings') await expect(page.getByTestId('language-switch')).toBeVisible();
  else await expect(page.getByRole('heading', { name: 'Mäher', exact: true })).toBeVisible();
}

/** What Chrome and Edge fire when the web app can be installed. */
async function fireInstallPrompt(page: Page): Promise<void> {
  await page.evaluate(() => {
    const event = Object.assign(new Event('beforeinstallprompt', { cancelable: true }), {
      prompt: () => Promise.resolve(),
      userChoice: Promise.resolve({ outcome: 'dismissed' }),
    });
    window.dispatchEvent(event);
  });
}

async function expectNoHint(page: Page): Promise<void> {
  await expect(page.getByTestId('install-hint')).toHaveCount(0);
  await expect(page.getByText(IOS_TEXT)).toHaveCount(0);
  await expect(page.getByText(PROMPT_TEXT)).toHaveCount(0);
}

test('iPhone Safari shows the share sheet hint', async () => {
  const page = await open({ userAgent: UA.iphoneSafari, touchPoints: 5 });
  await expect(page.getByTestId('install-hint')).toHaveAttribute('data-offer', 'ios');
  await expect(page.getByTestId('install-hint')).toContainText(IOS_TEXT);
  await page.close();
  const settings = await open({ userAgent: UA.iphoneSafari, touchPoints: 5 }, '/settings');
  await expect(settings.getByTestId('install-panel')).toContainText(IOS_TEXT);
  await settings.close();
});

test('iPad Safari (desktop user agent with touch) shows the share sheet hint', async () => {
  const page = await open({ userAgent: UA.ipadSafari, touchPoints: 5 });
  await expect(page.getByTestId('install-hint')).toHaveAttribute('data-offer', 'ios');
  await page.close();
});

test('the web app on the iPhone and iPad home screen shows no hint', async () => {
  let page = await open({ userAgent: UA.iphoneSafari, touchPoints: 5, navigatorStandalone: true });
  await expectNoHint(page);
  await page.close();
  page = await open({ userAgent: UA.ipadSafari, touchPoints: 5, displayMode: 'standalone' });
  await expectNoHint(page);
  await page.close();
});

test('the iOS app shell shows no hint and no install panel (iPhone and iPad)', async () => {
  for (const userAgent of [UA.iosShell, UA.ipadShell]) {
    const page = await open({ userAgent, touchPoints: 5 });
    await expectNoHint(page);
    await page.close();
    const settings = await open({ userAgent, touchPoints: 5 }, '/settings');
    await expect(settings.getByTestId('install-panel')).toHaveCount(0);
    await settings.close();
  }
});

test('the Android app shell ignores beforeinstallprompt', async () => {
  const page = await open({ userAgent: UA.androidShell, touchPoints: 5 });
  await fireInstallPrompt(page);
  await expectNoHint(page);
  await page.close();
  const settings = await open({ userAgent: UA.androidShell, touchPoints: 5 }, '/settings');
  await fireInstallPrompt(settings);
  await expect(settings.getByTestId('install-panel')).toHaveCount(0);
  await settings.close();
});

test('a Chrome or Edge tab on Windows offers installing only after beforeinstallprompt', async () => {
  const page = await open({ userAgent: UA.windowsEdge });
  await expectNoHint(page);
  await fireInstallPrompt(page);
  const hint = page.getByTestId('install-hint');
  await expect(hint).toHaveAttribute('data-offer', 'prompt');
  await expect(hint).toContainText(PROMPT_TEXT);
  await expect(hint.getByRole('button', { name: 'Installieren' })).toBeVisible();
  await expect(page.getByText(IOS_TEXT)).toHaveCount(0);
  // After the install the tab shows nothing, also on the next visit.
  await page.evaluate(() => window.dispatchEvent(new Event('appinstalled')));
  await expectNoHint(page);
  await page.reload();
  await ready(page, '/mowers');
  await expectNoHint(page);
  await page.close();
});

for (const displayMode of ['standalone', 'minimal-ui', 'window-controls-overlay'] as const) {
  test(`the installed Windows app (display-mode ${displayMode}) shows no hint`, async () => {
    const page = await open({ userAgent: UA.windowsEdge, displayMode });
    await fireInstallPrompt(page);
    await expectNoHint(page);
    await page.close();
  });
}
