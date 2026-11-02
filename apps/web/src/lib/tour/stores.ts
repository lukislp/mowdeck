/**
 * Where the guided tour keeps its outcome (ADR 0073), as pure combinations of stores:
 *
 * - a regular installation: per user in Identity (`dismissed_hints`);
 * - the shared demo user (ADR 0068): per browser only, since a server-side outcome would end the
 *   tour for every visitor;
 * - demo sandboxes (ADR 0076): per user again (each visitor has an own account), and per browser
 *   too, so a returning visitor whose new sandbox is fresh does not get the tour again.
 */
import type { TourOutcome } from './steps';
import type { TourStore } from './tour.svelte';

/** An outcome that says the tour was met already. */
function known(outcome: TourOutcome | null): outcome is Exclude<TourOutcome, 'new'> {
  return outcome === 'seen' || outcome === 'done' || outcome === 'skipped';
}

/**
 * The browser's outcome when it has one, else the user's; saves go to both. A failing browser
 * store never hides the user's outcome, and a save never rejects (`TourStore.save`).
 */
export function browserFirst(browser: TourStore, user: TourStore): TourStore {
  return {
    load: async () => {
      const local: TourOutcome | null = await browser.load().catch(() => null);
      return known(local) ? local : user.load();
    },
    save: async (outcome, context) => {
      await Promise.allSettled([browser.save(outcome, context), user.save(outcome, context)]);
    },
  };
}

/** Which store applies: the demo config decides, read when the store is used. */
export function pickStore(
  mode: () => 'user' | 'shared-demo' | 'sandbox',
  stores: { user: TourStore; browser: TourStore },
): TourStore {
  const sandbox = browserFirst(stores.browser, stores.user);
  const current = () => {
    const which = mode();
    return which === 'sandbox' ? sandbox : which === 'shared-demo' ? stores.browser : stores.user;
  };
  return {
    load: () => current().load(),
    save: (outcome, context) => current().save(outcome, context),
  };
}
