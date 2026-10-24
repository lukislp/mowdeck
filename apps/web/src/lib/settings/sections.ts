/**
 * The one settings page (`/settings`): the modules with `placement: 'settings'` become its
 * sections, in module order, each under its anchor (`section`). Their old routes
 * (`/account`, `/settings/users`, ...) forward to the anchor and keep the query, so links from
 * mails, Stripe's return URL (`/account?billing=success#plan`) and bookmarks still work.
 */
import type { AppModule } from '#lib/modules/types.js';

export const SETTINGS_ROUTE = '/settings';

export interface SettingsSection {
  /** Module id, which also picks the section component. */
  id: string;
  /** Anchor id on the page. */
  anchor: string;
  label: () => string;
}

/** The sections of the visible modules, in module order. */
export function settingsSections(visible: readonly AppModule[]): SettingsSection[] {
  return visible
    .filter((module) => module.placement === 'settings' && !!module.section)
    .sort((a, b) => a.order - b.order)
    .map((module) => ({ id: module.id, anchor: module.section ?? module.id, label: module.label }));
}

/** The address of a section: `/settings?<query>#<anchor>`. */
export function sectionHref(anchor: string, search = ''): string {
  const query = search && search !== '?' ? (search.startsWith('?') ? search : `?${search}`) : '';
  return `${SETTINGS_ROUTE}${query}#${anchor}`;
}

/**
 * Where an old route of a settings section forwards to: the settings page with the same query
 * and the section's anchor, or the anchor the old link already carried (`#plan` inside the
 * profile). Null for a path that is no settings section.
 */
export function forwardTarget(
  modules: readonly AppModule[],
  pathname: string,
  search = '',
  hash = '',
): string | null {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname;
  const module = modules.find(
    (candidate) => candidate.placement === 'settings' && candidate.route === path,
  );
  if (!module) return null;
  const own = hash.replace(/^#/, '');
  return sectionHref(own || (module.section ?? module.id), search);
}
