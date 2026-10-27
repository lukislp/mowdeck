/**
 * Used only by `playwright merge-reports --config=playwright.merge.config.ts <blob dir>` in CI
 * (job e2e-web of .github/workflows/ci.yml): the blob reports of the shards become one HTML
 * report, one JUnit file and one JSON file (input of scripts/flaky-summary.mjs).
 */
import { defineConfig } from '@playwright/test';

export default defineConfig({
  reporter: [
    ['list'],
    ['html', { open: 'never', outputFolder: 'merged-report/html' }],
    ['junit', { outputFile: 'merged-report/junit.xml' }],
    ['json', { outputFile: 'merged-report/results.json' }],
  ],
});
