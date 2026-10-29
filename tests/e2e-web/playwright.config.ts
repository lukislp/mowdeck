import { defineConfig, devices } from '@playwright/test';

/**
 * Phase 2 to Phase 4 gates against a throwaway Compose stack (web app at /, API at /api/v1 on the
 * same origin). Start it first: `pnpm e2e:stack up` (scripts/stack.mjs, Compose project mowdeck-e2e
 * at http://localhost:8480). The global setup refuses any stack without the e2e marker of
 * infra/compose/docker-compose.e2e.yml, so the suite never writes into the stack you use.
 */
const baseURL =
  process.env.MOWDECK_E2E_BASE_URL ?? `http://localhost:${process.env.MOWDECK_E2E_PORT ?? '8480'}`;

export default defineConfig({
  testDir: './tests',
  globalSetup: './global-setup.ts',
  timeout: 180_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  // CI runs the suite in shards (ci.yml, job e2e-web-shard): each writes a blob report, which
  // playwright.merge.config.ts turns into the one HTML/JUnit/JSON report.
  reporter: process.env.CI ? [['list'], ['blob']] : [['list']],
  use: {
    baseURL,
    locale: 'de-DE',
    timezoneId: 'Europe/Berlin',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'phone',
      testMatch:
        /(gate|spot-phone|layout-phone|aerial-desktop|api-tokens-phone|source|scroll)\.spec\.ts/,
      use: {
        ...devices['Pixel 7'],
        browserName: 'chromium',
        viewport: { width: 390, height: 844 },
      },
    },
    {
      name: 'desktop',
      testMatch:
        /(desktop|notifications|connection|schedules|drive|calibration|health|fleet|operator|camera|camera-freeze|camera-relay|camera-relay-web|vision|spot|source|scroll)\.spec\.ts/,
      use: { browserName: 'chromium', viewport: { width: 1280, height: 800 } },
    },
    // Safari's engine (ADR 0044 amendment 2026-10-17): the camera's freeze detection and the
    // drive view's hold over a frozen picture also in WebKit, the relay player (ADR 0045;
    // skipped where the WebKit build has no WebRTC, e.g. on Windows), the source code link and
    // the one scroller per page.
    {
      name: 'webkit',
      testMatch: /(camera-freeze|drive-camera|camera-relay-web|source|scroll)\.spec\.ts/,
      use: { browserName: 'webkit', viewport: { width: 1280, height: 800 } },
    },
    // Firefox: the relay player (ADR 0045) and the one scroller per page. The test stack offers its ICE port on 127.0.0.1 only;
    // Firefox reaches it only with loopback candidates allowed and its host addresses not hidden
    // behind mDNS names (a real relay offers a LAN address, see docs/dev/web-app.md). On Linux it
    // decodes the relay's H.264 only with the OpenH264 plugin in MOZ_GMP_PATH (README.md).
    {
      name: 'firefox',
      testMatch: /(camera-relay-web|scroll)\.spec\.ts/,
      use: {
        browserName: 'firefox',
        viewport: { width: 1280, height: 800 },
        launchOptions: {
          firefoxUserPrefs: {
            'media.peerconnection.ice.loopback': true,
            'media.peerconnection.ice.obfuscate_host_addresses': false,
          },
        },
      },
    },
    {
      name: 'screens',
      testMatch: /screens\.spec\.ts/,
      use: { browserName: 'chromium' },
    },
    // The public demo (ADR 0065): only against a demo stack (CI job e2e-demo); the tests skip
    // on every other stack. On a phone too, where the banner has its short text.
    {
      name: 'demo',
      testMatch: /demo\.spec\.ts/,
      use: { browserName: 'chromium', viewport: { width: 1280, height: 800 } },
    },
    {
      name: 'demo-phone',
      testMatch: /demo\.spec\.ts/,
      use: {
        ...devices['Pixel 7'],
        browserName: 'chromium',
        viewport: { width: 390, height: 844 },
      },
    },
  ],
});
