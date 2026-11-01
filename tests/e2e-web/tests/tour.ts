/**
 * Helpers of the guided tour specs (ADR 0073, `tour-*.spec.ts`). The suite turns the tour's
 * automatic start off for every other spec (playwright.config.ts, `storageState`); these specs
 * start with a clean browser instead (`FRESH_BROWSER`).
 */
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';

import type { Actor } from './support';

/** A browser that never saw MowDeck: no device switch, so the tour may start by itself. */
export const FRESH_BROWSER = { cookies: [], origins: [] };

export function tourOf(page: Page): { root: Locator; card: Locator; next: Locator } {
  return {
    root: page.getByTestId('tour'),
    card: page.getByTestId('tour-card'),
    next: page.getByTestId('tour-next'),
  };
}

/** The tour shows `step` with its target framed (or, for a centred step, without a frame). */
export async function expectStep(page: Page, step: string, framed = true): Promise<void> {
  const { root, card } = tourOf(page);
  await expect(root).toHaveAttribute('data-step', step, { timeout: 30_000 });
  await expect(card).toBeVisible();
  if (framed) await expect(page.getByTestId('tour-frame')).toBeVisible({ timeout: 15_000 });
}

/** The keys Identity keeps for this user (the tour's outcome is among them). */
export async function storedHints(request: APIRequestContext, who: Actor): Promise<string[]> {
  const response = await request.get('/api/v1/identity/me/onboarding', {
    headers: { Authorization: `Bearer ${who.token}` },
  });
  expect(response.status(), await response.text()).toBe(200);
  return ((await response.json()) as { dismissed_hints: string[] }).dismissed_hints;
}

/** The focus is on an element inside the tour's card. */
export async function focusInCard(page: Page): Promise<boolean> {
  return page.evaluate(
    () => !!document.querySelector('[data-testid="tour-card"]')?.contains(document.activeElement),
  );
}

/** Light and dark screenshots into MOWDECK_E2E_SHOTS (only when set). */
export async function shots(page: Page, name: string): Promise<void> {
  const dir = process.env.MOWDECK_E2E_SHOTS;
  if (!dir) return;
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${dir}/${name}-${scheme}.png`, animations: 'disabled' });
  }
  await page.emulateMedia({ colorScheme: 'light' });
}
