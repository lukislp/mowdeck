/**
 * The module registry of the shell (design 3.2): built-in route modules, enabled at runtime by
 * the `ui_areas` that the deployed services announce through Identity (`GET /ui-areas`).
 *
 * A built-in module is shown when the user holds its permission and either it is `core` or a
 * ui_area with the same route exists (the backing service is deployed). If the endpoint fails,
 * the menu falls back to the permission check alone; it never blocks the emergency stop.
 * Operator modules (`platformAdmin`) follow the operator probe of `$lib/operator/access`.
 */
import { identityApi, unwrap } from '$lib/api/client';
import type { AppModule } from '$lib/modules/types';

const found = import.meta.glob<{ default: AppModule }>('../modules/*/module.ts', { eager: true });

export const builtIn: AppModule[] = Object.values(found)
  .map((entry) => entry.default)
  .sort((a, b) => a.order - b.order);

export interface UiArea {
  route: string;
  module: string;
  required_permission: string;
}

/** The modules to show for `permissions` and the announced `areas` (null: unknown); modules of
 * operator admins only when `platformAdmin` (the probe succeeded). */
export function visibleModules(
  modules: AppModule[],
  permissions: readonly string[],
  areas: readonly UiArea[] | null,
  platformAdmin = false,
): AppModule[] {
  const routes = areas ? new Set(areas.map((a) => a.route)) : null;
  return modules.filter((module) => {
    if (module.platformAdmin && !platformAdmin) return false;
    if (module.requiredPermission && !permissions.includes(module.requiredPermission)) {
      return false;
    }
    return module.core === true || routes === null || routes.has(module.area ?? module.route);
  });
}

/** The entries of the navigation: the visible modules that are not settings sections. */
export function navModules(visible: AppModule[]): AppModule[] {
  return visible.filter((module) => (module.placement ?? 'primary') === 'primary');
}

class Registry {
  areas = $state<UiArea[] | null>(null);
  #loaded = false;

  async load(): Promise<void> {
    if (this.#loaded) return;
    this.#loaded = true;
    try {
      this.areas = await unwrap(identityApi.GET('/api/v1/identity/ui-areas'));
      const known = new Set(builtIn.flatMap((m) => [m.route, m.area ?? m.route]));
      for (const area of this.areas) {
        if (!known.has(area.route)) {
          // Expected for areas the app serves inside another page (e.g. /settings/audit).
          console.debug(`ui_area ${area.route} of ${area.module} has no built-in module`);
        }
      }
    } catch {
      this.areas = null;
    }
  }

  reset(): void {
    this.#loaded = false;
    this.areas = null;
  }
}

export const registry = new Registry();
