import { defineConfig, devices } from '@playwright/test';

/**
 * Phase 2 to Phase 4 gates against the Compose stack (web app at /, API at /api/v1 on the same origin).
 * Start the stack first: docker compose -f infra/compose/docker-compose.yml up -d --build --wait
 */
const baseURL = process.env.MOWDECK_E2E_BASE_URL ?? 'http://localhost';

export default defineConfig({
  testDir: './tests',
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
      testMatch: /gate\.spec\.ts/,
      use: {
        ...devices['Pixel 7'],
        browserName: 'chromium',
        viewport: { width: 390, height: 844 },
      },
    },
    {
      name: 'desktop',
      testMatch:
        /(desktop|notifications|connection|schedules|drive|health|fleet|operator|camera|vision|spot)\.spec\.ts/,
      use: { browserName: 'chromium', viewport: { width: 1280, height: 800 } },
    },
    {
      name: 'screens',
      testMatch: /screens\.spec\.ts/,
      use: { browserName: 'chromium' },
    },
  ],
});
