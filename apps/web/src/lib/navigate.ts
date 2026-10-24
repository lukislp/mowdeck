/**
 * Client-side navigation to a path that comes from outside the code: a `?next=` parameter, a
 * tapped notification, a deep link of the native shell, a message of the service worker.
 *
 * SvelteKit 3's `goto` rejects a path that matches no route (SvelteKit 2 rendered the 404 page).
 * Such a path is loaded as a full page instead, so the SPA fallback (`200.html`) shows the same
 * "not found" page as before and a stale link never ends in an unhandled rejection.
 */
import { goto, type GotoOptions } from '$app/navigation';
import { match } from '$app/paths';

export async function navigatePath(path: string, options: GotoOptions = {}): Promise<void> {
  if (await match(path)) {
    await goto(path, options);
    return;
  }
  if (options.replace) location.replace(path);
  else location.assign(path);
}
