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
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
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
      testMatch: /(gate|spot-phone|layout-phone)\.spec\.ts/,
      use: {
        ...devices['Pixel 7'],
        browserName: 'chromium',
        viewport: { width: 390, height: 844 },
      },
    },
    {
      name: 'desktop',
      testMatch:
        /(desktop|notifications|connection|schedules|drive|calibration|health|fleet|operator|camera|camera-freeze|vision|spot)\.spec\.ts/,
      use: { browserName: 'chromium', viewport: { width: 1280, height: 800 } },
    },
    // Safari's engine (ADR 0044 amendment 2026-10-17): the camera's freeze detection and the
    // drive view's hold over a frozen picture also in WebKit.
    {
      name: 'webkit',
      testMatch: /(camera-freeze|drive-camera)\.spec\.ts/,
      use: { browserName: 'webkit', viewport: { width: 1280, height: 800 } },
    },
    {
      name: 'screens',
      testMatch: /screens\.spec\.ts/,
      use: { browserName: 'chromium' },
    },
  ],
});
