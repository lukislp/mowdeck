/**
 * The API explorer (ADR 0072) is off on a regular installation: /config.json says so, the frame's
 * files (the Scalar bundle included) are not served, and the page is a 404. Skips on a stack that
 * turned the explorer on (the demo).
 */
import { expect, test } from '@playwright/test';

test('the API explorer and its files are not served unless turned on', async ({ request }) => {
  const config = (await (await request.get('/config.json')).json()) as Record<string, unknown>;
  test.skip(config.apiExplorer === true, 'the explorer is on for this stack');
  expect(config.apiExplorer).toBe(false);
  for (const path of [
    '/_explorer/index.html',
    '/_explorer/scalar.js',
    '/_explorer/openapi/fleet.json',
  ]) {
    expect((await request.get(path)).status(), path).toBe(404);
  }
});
